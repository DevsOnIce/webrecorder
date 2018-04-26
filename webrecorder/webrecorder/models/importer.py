from tempfile import SpooledTemporaryFile, NamedTemporaryFile
from bottle import request

from warcio.archiveiterator import ArchiveIterator
from warcio.limitreader import LimitReader

from har2warc.har2warc import HarParser
from warcio.warcwriter import BufferWARCWriter, WARCWriter
from warcio.timeutils import iso_date_to_datetime


from pywb.warcserver.index.cdxobject import CDXObject

import traceback
import json
import requests
import atexit

import base64
import os
import gevent
import redis

from webrecorder.utils import SizeTrackingReader, CacheingLimitReader
from webrecorder.utils import redis_pipeline, sanitize_title

import logging
logger = logging.getLogger(__name__)


BLOCK_SIZE = 16384 * 8
EMPTY_DIGEST = '3I42H3S6NNFQ2MSVX7XZKYAYSCX5QBYJ'


# ============================================================================
class ImportStatusChecker(object):
    UPLOAD_KEY = 'u:{user}:upl:{upid}'
    UPLOAD_EXP = 120

    def __init__(self, redis):
        self.redis = redis

    def get_upload_status(self, user, upload_id):
        upload_key = self.UPLOAD_KEY.format(user=user.name, upid=upload_id)

        props = self.redis.hgetall(upload_key)
        if not props:
            return {}

        props['user'] = user.name
        props['upload_id'] = upload_id

        total_size = props.get('total_size')
        if not total_size:
            return props

        self.redis.expire(upload_key, self.UPLOAD_EXP)
        props['total_size'] = int(total_size)
        props['size'] = int(props.get('size', 0))
        props['files'] = int(props['files'])
        props['total_files'] = int(props['total_files'])

        if props.get('files') == 0:
            props['size'] = props['total_size']

        return props


# ============================================================================
class BaseImporter(ImportStatusChecker):
    def __init__(self, redis, config, wam_loader=None):
        super(BaseImporter, self).__init__(redis)
        self.config = config
        self.wam_loader = wam_loader

        self.cdxj_key = config['cdxj_key_templ']

        self.upload_path = config['url_templates']['upload']
        self.upload_exp = int(config['upload_status_expire'])

        self.record_host = os.environ['RECORD_HOST']

        self.upload_coll_info = config['upload_coll']

        self.max_detect_pages = config['max_detect_pages']

    def handle_upload(self, stream, upload_id, upload_key, infos, filename,
                      user, force_coll_name, total_size):

        logger.debug('Begin handle_upload() from: ' + filename + ' force_coll_name: ' + str(force_coll_name))

        num_recs = 0
        num_recs = len(infos)
        # first info is for collection, not recording
        if num_recs >= 2:
            num_recs -= 1

        logger.debug('Parsed {0} recordings, Buffer Size {1}'.format(num_recs, total_size))

        first_coll, rec_infos = self.process_upload(user, force_coll_name, infos, stream,
                                                    filename, total_size, num_recs)

        if not rec_infos:
            print('NO ARCHIVES!')
            #stream.close()
            return {'error_message': 'No Archive Data Found'}

        with redis_pipeline(self.redis) as pi:
            pi.hset(upload_key, 'coll', first_coll.name)
            pi.hset(upload_key, 'coll_title', first_coll.get_prop('title'))
            pi.hset(upload_key, 'filename', filename)
            pi.expire(upload_key, self.upload_exp)

        self.launch_upload(self.run_upload,
                           upload_key,
                           filename,
                           stream,
                           user,
                           rec_infos,
                           total_size)

        return {'upload_id': upload_id,
                'user': user.name
               }

    def _init_upload_status(self, user, total_size, num_files, filename=None, expire=None):
        upload_id = self._get_upload_id()

        upload_key = self.UPLOAD_KEY.format(user=user.name, upid=upload_id)

        with redis_pipeline(self.redis) as pi:
            pi.hset(upload_key, 'size', 0)
            pi.hset(upload_key, 'total_size', total_size * 2)
            pi.hset(upload_key, 'total_files', num_files)
            pi.hset(upload_key, 'files', num_files)

            if filename:
                pi.hset(upload_key, 'filename', filename)

            if expire:
                pi.expire(upload_key, expire)

        return upload_id, upload_key

    def run_upload(self, upload_key, filename, stream, user, rec_infos, total_size):
        try:
            count = 0
            num_recs = len(rec_infos)
            last_end = 0

            for info in rec_infos:
                count += 1
                logger.debug('Id: {0}, Uploading Rec {1} of {2}'.format(upload_key, count, num_recs))

                if info['length'] > 0:
                    self.do_upload(upload_key,
                                   filename,
                                   stream,
                                   user.name,
                                   info['coll'],
                                   info['rec'],
                                   info['offset'],
                                   info['length'])
                else:
                    logger.debug('SKIP upload for zero-length recording')


                pages = info.get('pages')
                if pages is None:
                    pages = self.detect_pages(info['coll'], info['rec'])

                if pages:
                    info['recording'].import_pages(pages)

                diff = info['offset'] - last_end
                last_end = info['offset'] + info['length']
                if diff > 0:
                    self._add_split_padding(diff, upload_key)

        except:
            traceback.print_exc()

        finally:
            # add remainder of file, assumed consumed/skipped, if any
            last_end = stream.tell()
            stream.close()

            if last_end < total_size:
                diff = total_size - last_end
                self._add_split_padding(diff, upload_key)

            with redis_pipeline(self.redis) as pi:
                pi.hincrby(upload_key, 'files', -1)
                pi.hset(upload_key, 'done', 1)

    def har2warc(self, filename, stream):
        out = self._har2warc_temp_file()
        writer = WARCWriter(out)

        buff_list = []
        while True:
            buff = stream.read()
            if not buff:
                break

            buff_list.append(buff.decode('utf-8'))

        #wrapper = TextIOWrapper(stream)
        try:
            rec_title = filename.rsplit('/', 1)[-1]
            har = json.loads(''.join(buff_list))
            HarParser(har, writer).parse(filename + '.warc.gz', rec_title)
        finally:
            stream.close()

        size = out.tell()
        out.seek(0)
        return out, size

    def process_upload(self, user, force_coll_name, infos, stream, filename, total_size, num_recs):
        stream.seek(0)

        count = 0

        first_coll = None

        collection = None
        recording = None

        if force_coll_name:
            collection = user.get_collection_by_name(force_coll_name)

        rec_infos = []

        for info in infos:
            type = info.get('type')

            if type == 'collection':
                if not collection:
                    collection = self.make_collection(user, filename, info)

            elif type == 'recording':
                if not collection:
                    collection = self.make_collection(user, filename, self.upload_coll_info)

                desc = info.get('desc', '')

                recording = collection.create_recording(title=info.get('title', ''),
                                                        desc=desc,
                                                        rec_type=info.get('rec_type'),
                                                        ra_list=info.get('ra'))

                info['id'] = recording.my_id

                count += 1
                #yield collection, recording

                logger.debug('Processing Upload Rec {0} of {1}'.format(count, num_recs))

                rec_infos.append({'coll': collection.my_id,
                                  'rec': recording.my_id,
                                  'offset': info['offset'],
                                  'length': info['length'],
                                  'pages': info.get('pages', None),
                                  'collection': collection,
                                  'recording': recording,
                                 })

                self.set_date_prop(recording, info, 'created_at', 'created_at_date')
                self.set_date_prop(recording, info, 'updated_at', 'updated_at_date')

            if not first_coll:
                first_coll = collection

        return first_coll, rec_infos

    def detect_pages(self, coll, rec):
        key = self.cdxj_key.format(coll=coll, rec=rec)

        pages = []

        #for member, score in self.redis.zscan_iter(key):
        for member in self.redis.zrange(key, 0, -1):
            cdxj = CDXObject(member.encode('utf-8'))

            if ((not self.max_detect_pages or len(pages) < self.max_detect_pages)
                and self.is_page(cdxj)):
                pages.append(dict(url=cdxj['url'],
                                  title=cdxj['url'],
                                  timestamp=cdxj['timestamp']))

        return pages

    def is_page(self, cdxj):
        if cdxj['url'].endswith('/robots.txt'):
            return False

        if not cdxj['url'].startswith(('http://', 'https://')):
            return False

        status = cdxj.get('status', '-')

        if (cdxj['mime'] in ('text/html', 'text/plain')  and
            status in ('200', '-') and
            cdxj['digest'] != EMPTY_DIGEST):


            if status == '200':
                # check for very long query, greater than the rest of url -- probably not a page
                parts = cdxj['url'].split('?', 1)
                if len(parts) == 2 and len(parts[1]) > len(parts[0]):
                    return False

            return True

        return False

    def parse_uploaded(self, stream, expected_size):
        arciterator = ArchiveIterator(stream,
                                      no_record_parse=True,
                                      verify_http=True,
                                      block_size=BLOCK_SIZE)
        infos = []

        last_indexinfo = None
        indexinfo = None
        is_first = True
        remote_archives = None

        for record in arciterator:
            warcinfo = None
            if record.rec_type == 'warcinfo':
                try:
                    warcinfo = self.parse_warcinfo(record)
                except Exception as e:
                    print('Error Parsing WARCINFO')
                    traceback.print_exc()

            elif remote_archives is not None:
                source_uri = record.rec_headers.get('WARC-Source-URI')
                if source_uri:
                    if self.wam_loader:
                        res = self.wam_loader.find_archive_for_url(source_uri)
                        if res:
                            remote_archives.add(res[2])

            arciterator.read_to_end(record)

            if last_indexinfo:
                last_indexinfo['offset'] = arciterator.member_info[0]
                last_indexinfo = None

            if warcinfo:
                self.add_index_info(infos, indexinfo, arciterator.member_info[0])

                indexinfo = warcinfo.get('json-metadata')
                indexinfo['offset'] = None

                if 'title' not in indexinfo:
                    indexinfo['title'] = 'Uploaded Recording'

                if 'type' not in indexinfo:
                    indexinfo['type'] = 'recording'

                indexinfo['ra'] = set()
                remote_archives = indexinfo['ra']

                last_indexinfo = indexinfo

            elif is_first:
                indexinfo = {'type': 'recording',
                             'title': 'Uploaded Recording',
                             'offset': 0,
                            }

            is_first = False

        if indexinfo:
            self.add_index_info(infos, indexinfo, stream.tell())

        # if anything left over, likely due to WARC error, consume remainder
        if stream.tell() < expected_size:
            while True:
                buff = stream.read(8192)
                if not buff:
                    break

        return infos

    def add_index_info(self, infos, indexinfo, curr_offset):
        if not indexinfo or indexinfo.get('offset') is None:
            return

        indexinfo['length'] = curr_offset - indexinfo['offset']

        infos.append(indexinfo)

    def parse_warcinfo(self, record):
        valid = False
        warcinfo = {}
        warcinfo_buff = record.raw_stream.read(record.length)
        warcinfo_buff = warcinfo_buff.decode('utf-8')
        for line in warcinfo_buff.rstrip().split('\n'):
            parts = line.split(':', 1)

            if parts[0] == 'json-metadata':
                warcinfo['json-metadata'] = json.loads(parts[1])
                valid = True
            else:
                warcinfo[parts[0]] = parts[1].strip()

        # ignore if no json-metadata or doesn't contain type of colleciton or recording
        return warcinfo if valid else None

    def set_date_prop(self, obj, info, ts_prop, iso_prop):

        # first check the iso_prop field
        value = info.get(iso_prop)
        if value:
            # convert back to seconds
            dt = iso_date_to_datetime(value)
            value = dt.timestamp()
        else:
            # use seconds field, if set
            value = info.get(ts_prop)

        if value is not None:
            value = int(value)
            obj.set_prop(ts_prop, value)

    def do_upload(self, upload_key, filename, stream, user, coll, rec, offset, length):
        raise NotImplemented()

    def launch_upload(self, func, *args):
        raise NotImplemented()

    def _get_upload_id(self):
        raise NotImplemented()

    def is_public(self, info):
        raise NotImplemented()

    def _add_split_padding(self, diff, upload_key):
        raise NotImplemented()

    def _har2warc_temp_file(self):
        raise NotImplemented()

    def make_collection(self, user, filename, info):
        raise NotImplemented()


# ============================================================================
class UploadImporter(BaseImporter):
    def upload_file(self, user, stream, expected_size, filename, force_coll_name=''):
        temp_file = None
        logger.debug('Upload Begin')

        logger.debug('Expected Size: ' + str(expected_size))

        #is_anon = False

        size_rem = user.get_size_remaining()

        logger.debug('User Size Rem: ' + str(size_rem))

        if size_rem < expected_size:
            return {'error_message': 'Sorry, not enough space to upload this file'}

        if force_coll_name and not user.has_collection(force_coll_name):
            #if is_anon:
            #    user.create_collection(force_coll, 'Temporary Collection')

            #else:
            status = 'Collection {0} not found'.format(force_coll_name)
            return {'error_message': status}

        temp_file = SpooledTemporaryFile(max_size=BLOCK_SIZE)

        stream = CacheingLimitReader(stream, expected_size, temp_file)

        if filename.endswith('.har'):
            stream, expected_size = self.har2warc(filename, stream)
            temp_file.close()
            temp_file = stream

        infos = self.parse_uploaded(stream, expected_size)

        total_size = temp_file.tell()
        if total_size != expected_size:
            return {'error_message': 'size mismatch: expected {0}, got {1}'.format(expected_size, total_size)}

        upload_id, upload_key = self._init_upload_status(user, total_size, 1, filename=filename)

        return self.handle_upload(temp_file, upload_id, upload_key, infos, filename,
                                  user, force_coll_name, total_size)

    def do_upload(self, upload_key, filename, stream, user, coll, rec, offset, length):
        stream.seek(offset)

        logger.debug('do_upload(): {0} offset: {1}: len: {2}'.format(rec, offset, length))

        stream = LimitReader(stream, length)
        headers = {'Content-Length': str(length)}

        upload_url = self.upload_path.format(record_host=self.record_host,
                                             user=user,
                                             coll=coll,
                                             rec=rec,
                                             upid=upload_key)

        r = requests.put(upload_url,
                         headers=headers,
                         data=stream)

    def _get_upload_id(self):
        return base64.b32encode(os.urandom(5)).decode('utf-8')

    def is_public(self, info):
        return info.get('public', False)

    def _add_split_padding(self, diff, upload_key):
        self.redis.hincrby(upload_key, 'size', diff * 2)

    def _har2warc_temp_file(self):
        return SpooledTemporaryFile(max_size=BLOCK_SIZE)

    def launch_upload(self, func, *args):
        gevent.spawn(func, *args)

    def make_collection(self, user, filename, info):
        desc = info.get('desc', '').format(filename=filename)
        public = self.is_public(info)

        info['id'] = sanitize_title(info['title'])
        collection = user.create_collection(info['id'],
                                       title=info['title'],
                                       desc=desc,
                                       public=public,
                                       allow_dupe=True)

        info['id'] = collection.name
        info['type'] = 'collection'

        self.set_date_prop(collection, info, 'created_at', 'created_at_date')
        self.set_date_prop(collection, info, 'updated_at', 'updated_at_date')

        return collection


# ============================================================================
class InplaceImporter(BaseImporter):
    def __init__(self, redis, config, user, indexer, upload_id, create_coll=True):
        wam_loader = indexer.wam_loader if indexer else None
        super(InplaceImporter, self).__init__(redis, config, wam_loader)
        self.indexer = indexer
        self.upload_id = upload_id

        if not create_coll:
            self.the_collection = None
            return

        self.the_collection = user.create_collection(self.upload_coll_info['id'],
                                                     title=self.upload_coll_info['title'],
                                                     desc=self.upload_coll_info['desc'],
                                                     public=self.upload_coll_info['public'])

    def multifile_upload(self, user, files):
        total_size = 0

        for filename in files:
            total_size += os.path.getsize(filename)

        upload_id, upload_key = self._init_upload_status(user, total_size,
                                                         num_files=len(files),
                                                         expire=self.upload_exp)

        gevent.sleep(0)

        for filename in files:
            size = 0
            fh = None
            try:
                size = os.path.getsize(filename)
                fh = open(filename, 'rb')

                self.redis.hset(upload_key, 'filename', filename)

                stream = SizeTrackingReader(fh, size, self.redis, upload_key)

                if filename.endswith('.har'):
                    stream, expected_size = self.har2warc(filename, stream)
                    fh.close()
                    fh = stream
                    atexit.register(lambda: os.remove(stream.name))

                infos = self.parse_uploaded(stream, size)

                res = self.handle_upload(fh, upload_id, upload_key, infos, filename,
                                         user, False, size)

                assert('error_message' not in res)
            except Exception as e:
                traceback.print_exc()
                print('ERROR PARSING: ' + filename)
                print(e)
                if fh:
                    rem = size - fh.tell()
                    if rem > 0:
                        self.redis.hincrby(upload_key, 'size', rem)
                    self.redis.hincrby(upload_key, 'files', -1)
                    fh.close()

    def do_upload(self, upload_key, filename, stream, user, coll, rec, offset, length):
        stream.seek(offset)

        if hasattr(stream, 'name'):
            filename = stream.name

        params = {'param.user': user,
                  'param.coll': coll,
                  'param.rec': rec,
                  'param.upid': upload_key,
                 }

        self.indexer.add_warc_file(filename, params)
        self.indexer.add_urls_to_index(stream, params, filename, length)

    def _get_upload_id(self):
        return self.upload_id

    def is_public(self, info):
        return True

    def _add_split_padding(self, diff, upload_key):
        self.redis.hincrby(upload_key, 'size', diff)

    def _har2warc_temp_file(self):
        return NamedTemporaryFile(suffix='.warc.gz', delete=False)

    def launch_upload(self, func, *args):
        func(*args)

    def make_collection(self, user, filename, info):
        if info.get('title') == 'Temporary Collection':
            info['title'] = 'Collection'
            if not info.get('desc'):
                info['desc'] = self.upload_coll_info.get('desc', '').format(filename=filename)

        self.the_collection.set_prop('title', info['title'])
        self.the_collection.set_prop('desc', info['desc'])
        return self.the_collection

