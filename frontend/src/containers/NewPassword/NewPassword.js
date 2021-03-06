import React from 'react';
import { connect } from 'react-redux';

import { setNewPassword } from 'redux/modules/passwordReset';
import { showModal } from 'redux/modules/userLogin';

import { NewPasswordUI } from 'components/siteComponents';


const mapStateToProps = ({ app }) => {
  return {
    errors: app.getIn(['passwordReset', 'errors']),
    success: app.getIn(['passwordReset', 'setNew'])
  };
};

const mapDispatchToProps = (dispatch) => {
  return {
    setPassword: data => dispatch(setNewPassword(data)),
    toggleLogin: () => dispatch(showModal(true))
  };
};


export default connect(
  mapStateToProps,
  mapDispatchToProps
)(NewPasswordUI);
