import React from 'react';
import { connect } from 'react-redux';

import { showModal } from 'redux/modules/userLogin';
import { load } from 'redux/modules/auth';

import { TempUsageUI } from 'components/siteComponents';


const mapStateToProps = ({ app }) => {
  return {
    tempUser: app.getIn(['auth', 'user'])
  };
};

const mapDispatchToProps = (dispatch) => {
  return {
    hideModal: () => dispatch(showModal(false)),
    loadUsage: () => dispatch(load())
  };
};

export default connect(
  mapStateToProps,
  mapDispatchToProps
)(TempUsageUI);
