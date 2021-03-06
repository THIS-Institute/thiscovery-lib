#
#   Thiscovery API - THIS Institute’s citizen science platform
#   Copyright (C) 2019 THIS Institute
#
#   This program is free software: you can redistribute it and/or modify
#   it under the terms of the GNU Affero General Public License as
#   published by the Free Software Foundation, either version 3 of the
#   License, or (at your option) any later version.
#
#   This program is distributed in the hope that it will be useful,
#   but WITHOUT ANY WARRANTY; without even the implied warranty of
#   MERCHANTABILITY or FITNESS FOR A PARTICULAR PURPOSE.  See the
#   GNU Affero General Public License for more details.
#
#   A copy of the GNU Affero General Public License is available in the
#   docs folder of this project.  It is also available www.gnu.org/licenses/
#
import functools
import http
import json
import requests
from http import HTTPStatus
from datetime import datetime, timezone

from thiscovery_lib import dynamodb_utilities as ddb_utils
import thiscovery_lib.utilities as utils
from thiscovery_lib.utilities import get_secret, get_logger, get_aws_namespace, DetailedValueError, now_with_tz


DATE_FORMAT = '%Y-%m-%d %H:%M:%S'

# use namespace_override to enable using dev hubspot with production Thiscovery
# hubspot_connection = get_secret('hubspot-connection', namespace_override='/dev/')
#
# client_id = hubspot_connection['client-id']
# client_secret = hubspot_connection['client-secret']

BASE_URL = 'https://api.hubapi.com'
MOCK_BASE_URL = 'https://0ed709fe-f683-460b-843b-844744e419f9.mock.pstmn.io'
CONTACTS_ENDPOINT = '/contacts/v1'
INTEGRATIONS_ENDPOINT = '/integrations/v1'
TASK_SIGNUP_TLE_TYPE_NAME = 'task-signup'


# region decorators
def hubspot_api_error_handler(func):
    @functools.wraps(func)
    def wrapper(*args, **kwargs):
        status_code = func(*args, **kwargs)
        if status_code == http.HTTPStatus.NO_CONTENT:
            return status_code
        elif status_code == http.HTTPStatus.BAD_REQUEST:
            raise utils.DetailedValueError('Received a BAD REQUEST (400) response from the HubSpot API',
                                           details={'result': status_code})
        elif status_code == http.HTTPStatus.UNAUTHORIZED:
            raise utils.DetailedValueError('Received a UNAUTHORIZED (401) response from the HubSpot API',
                                           details={'result': status_code})
        elif status_code == http.HTTPStatus.NOT_FOUND:
            raise utils.DetailedValueError('Received a NOT FOUND (404) response from the HubSpot API',
                                           details={'result': status_code})
        elif status_code == http.HTTPStatus.INTERNAL_SERVER_ERROR:
            raise utils.DetailedValueError('Received a INTERNAL SERVER ERROR (500) response from the HubSpot API',
                                           details={'result': status_code})
        else:
            raise utils.DetailedValueError('Received an error from the HubSpot API',
                                           details={'result': status_code})
    return wrapper
# endregion


class HubSpotClient:
    tokens_table_name = 'tokens'
    token_item_id = 'hubspot'
    expired_token_item_id = 'hubspot-expired'
    token_item_type = 'oAuth_token'
    app_id_secret_name = 'app-id'
    client_id_secret_name = 'client-id'
    client_secret_name = 'client-secret'

    def __init__(self, mock_server=False, correlation_id=None, stack_name='thiscovery-core'):
        self.mock_server = mock_server
        self.logger = get_logger()
        self.correlation_id = correlation_id
        self.ddb = ddb_utils.Dynamodb(stack_name=stack_name)
        self.tokens = self.get_token_from_database()

        if not self.tokens:
            self.access_token, self.refresh_token = None, None
        else:
            self.access_token = self.tokens['access_token']
            self.refresh_token = self.tokens['refresh_token']

        self.connection_secret = None
        self.app_id = None

    # region token management
    def get_token_from_database(self, item_name=None):
        if item_name is None:
            item_name = self.token_item_id

        try:
            return self.ddb.get_item(self.tokens_table_name, item_name, self.correlation_id)['details']
        except:
            self.logger.warning(f'could not retrieve hubspot token from dynamodb item {item_name}')
            return None

    def create_expired_token_item(self):
        """
        Creates an hubspot-expired item in Dynamodb with the value of existing token (hubspot). Notice that the expired token might still be valid on
        creation.

        Returns:

        """
        token = self.get_token_from_database()
        if not token:
            raise utils.ObjectDoesNotExistError('Hubspot token not found', details={'correlation_id': self.correlation_id})
        response = self.save_token(token, item_name=self.expired_token_item_id)
        # todo: add response check here; for now log its value
        self.logger.debug('put_item response', extra={'response': response, 'correlation_id': self.correlation_id})
        return token

    def get_expired_token_from_database(self):
        """
        An expired token is useful only for testing.
        """
        expired_token = self.get_token_from_database(item_name=self.expired_token_item_id)
        if expired_token is None:
            self.logger.warning('Could not find an expired token; creating one now so returned token might still be valid')
            expired_token = self.create_expired_token_item()
        return expired_token

    def get_hubspot_connection_secret(self):
        """
        Fetches HubSpot connection secret from AWS (or from class instance if fetched previously).

        Returns:
            HubSpot connection secret
        """
        if self.connection_secret is None:
            self.connection_secret = get_secret('hubspot-connection')
            self.app_id = self.connection_secret[self.app_id_secret_name]
        return self.connection_secret

    def get_new_token_from_hubspot(self, refresh_token='self value', code=None, redirect_url=None):
        """
        Use this function to renew the HubSpot token.

        Args:
            refresh_token: HubSpot refresh token stored in AWS Secrets Manager; default value maps to self.refresh_token
            code: HubSpot authorization code required to obtain initial token (one-off use)
            redirect_url: URL for authorization code delivery (one-off use)

        Returns:
            Dict of HubSpot credentials, containing values for keys 'access_token', 'refresh_token' and 'app-id'

        Notes:
            Saves the values of 'access_token', 'refresh_token' and 'app-id' to class instance attributes
        """
        if refresh_token == 'self value':
            refresh_token = self.refresh_token

        hubspot_connection = self.get_hubspot_connection_secret()
        client_id = hubspot_connection[self.client_id_secret_name]
        client_secret = hubspot_connection[self.client_secret_name]
        self.app_id = hubspot_connection[self.app_id_secret_name]

        formData = {
            "client_id": client_id,
            "client_secret": client_secret,
        }

        if redirect_url is not None:
            formData['redirect_uri'] = redirect_url

        if refresh_token:
            formData['grant_type'] = "refresh_token"
            formData['refresh_token'] = refresh_token
        else:
            formData['grant_type'] = "authorization_code"
            formData['code'] = code

        res = requests.post('https://api.hubapi.com/oauth/v1/token', data=formData)
        self.tokens = res.json()
        self.access_token = self.tokens['access_token']
        self.refresh_token = self.tokens['refresh_token']

        self.save_token(self.tokens)
        return {**self.tokens, 'app-id': self.app_id}

    def get_initial_token_from_hubspot(self):
        """
        One-off function to obtain initial HubSpot token

        Returns:
            HubSpot token (dict), containing values for keys 'access_token' and 'refresh_token'
        """
        from local.dev_config import INITIAL_HUBSPOT_AUTH_CODE, NGROK_URL_ID

        redirect_url = 'https://' + NGROK_URL_ID + '.ngrok.io/hubspot'
        return self.get_new_token_from_hubspot(
            refresh_token=None,
            code=INITIAL_HUBSPOT_AUTH_CODE,
            redirect_url=redirect_url
        )

    def save_token(self, new_token, item_name=None):
        if item_name is None:
            item_name = self.token_item_id
        return self.ddb.put_item(self.tokens_table_name, item_name, self.token_item_type, new_token, dict(),
                                 update_allowed=True, correlation_id=self.correlation_id)
    # endregion

    # region get/post/put/delete requests
    def get_token_request_headers(self):
        return {
            'Content-Type': 'application/json',
            'Authorization': f'Bearer {self.access_token}',
        }

    def hubspot_token_request(self, method, url, params={}, data={}):
        """
        Method for requests using token
        """

        if not self.access_token:
            self.get_new_token_from_hubspot()

        success = False
        retry_count = 0
        base_url = BASE_URL
        if self.mock_server:
            base_url = MOCK_BASE_URL
        full_url = base_url + url
        while not success:
            headers = self.get_token_request_headers()
            result = requests.request(
                method=method,
                url=full_url,
                params=params,
                headers=headers,
                data=json.dumps(data),
            )
            self.logger.info('Logging request and result',
                             extra={
                                 'request': {
                                     'method': method,
                                     'url': full_url,
                                     'data': data,
                                 },
                                 'result': result.text
                             })
            if method in ['POST', 'PUT', 'DELETE']:
                if result.status_code in [HTTPStatus.OK, HTTPStatus.NO_CONTENT, HTTPStatus.CREATED]:
                    success = True
                elif result.status_code == HTTPStatus.UNAUTHORIZED and retry_count <= 1:
                    self.get_new_token_from_hubspot(self.refresh_token)
                    retry_count += 1
                    # and loop to retry
                else:
                    errorjson = {'url': url, 'result': result, 'content': result.content}
                    raise DetailedValueError('Hubspot call returned HTTP code ' + str(result.status_code), errorjson)
            elif method in ['GET']:
                if result.status_code in [HTTPStatus.NOT_FOUND]:
                    self.logger.warning(f'Content not found; returning None',
                                        extra={'result.status_code': result.status_code, 'result.content': result.content})
                    return None
                else:
                    result = result.json()
                success = True
            else:
                raise DetailedValueError(f'Support for method {method} not implemented in {__file__}')

        return result

    def get(self, url):
        return self.hubspot_token_request('GET', url)

    def post(self, url: str, data: dict):
        return self.hubspot_token_request('POST', url, data=data)

    def put(self, url: str, data: dict):
        return self.hubspot_token_request('PUT', url, data=data)

    def delete(self, url):
        return self.hubspot_token_request('DELETE', url)
    # endregion

    # region hubspot developer get/post/put/delete methods - used for managing TLE definitions
    def hubspot_dev_request(self, method, url, data={}):
        """
        Make requests using developer API key and user id instead of usual oAuth2 token
        This is necessary for creating TLE types
        """
        from local.secrets import HUBSPOT_DEVELOPER_APIKEY, HUBSPOT_DEVELOPER_USERID
        if self.app_id is None:
            self.get_hubspot_connection_secret()
        full_url = BASE_URL + url
        params = {
            'hapikey': HUBSPOT_DEVELOPER_APIKEY,
            'userId': HUBSPOT_DEVELOPER_USERID,
            'application-id': self.app_id,
        }
        headers = {
            'Content-Type': 'application/json',
        }

        result = requests.request(
            method=method,
            url=full_url,
            params=params,
            headers=headers,
            data=json.dumps(data),
        )
        self.logger.info('Logging request and result',
                         extra={
                             'request': {
                                 'method': method,
                                 'url': full_url,
                                 'data': data,
                             },
                             'result': result.text
                         })
        if method in ['POST', 'PUT', 'DELETE']:
            if result.status_code not in [HTTPStatus.OK, HTTPStatus.NO_CONTENT, HTTPStatus.CREATED]:
                errorjson = {
                    'url': url,
                    'result': result,
                    'content': result.content
                }
                raise DetailedValueError('Hubspot API call returned HTTP code ' + str(result.status_code), errorjson)
        elif method in ['GET']:
            if result.status_code in [HTTPStatus.NOT_FOUND]:
                self.logger.warning(f'Content not found; returning None',
                                    extra={
                                        'result.status_code': result.status_code,
                                        'result.content': result.content
                                    })
                return None
            else:
                result = result.json()
        else:
            raise DetailedValueError(f'Support for method {method} not implemented in {__file__}')

        return result

    def developer_get(self, url: str):
        return self.hubspot_dev_request('GET', url)

    def developer_post(self, url: str, data: dict):
        return self.hubspot_dev_request('POST', url, data=data)

    def developer_delete(self, url: str):
        return self.hubspot_dev_request('DELETE', url)
    # endregion

    # region Contacts API methods
    def get_hubspot_contacts(self):
        url = f'{CONTACTS_ENDPOINT}/lists/all/contacts/all'
        return self.get(url)

    def get_hubspot_contact_by_id(self, id_):
        url = f'{CONTACTS_ENDPOINT}/contact/vid/{id_}/profile'
        return self.get(url)

    def get_hubspot_contact_by_email(self, email: str):
        url = f'{CONTACTS_ENDPOINT}/contact/email/{email}/profile'
        return self.get(url)

    @staticmethod
    def get_contact_property(contact, property_name):
        return contact['properties'][property_name]['value']

    @hubspot_api_error_handler
    def update_contact_core(self, url, property_changes):
        data = {"properties": property_changes}
        r = self.post(url, data)
        return r.status_code

    def update_contact_by_email(self, email: str, property_changes: list):
        url = f'{CONTACTS_ENDPOINT}/contact/email/{email}/profile'
        return self.update_contact_core(url, property_changes)

    def update_contact_by_id(self, hubspot_id, property_changes: list):
        url = f'{CONTACTS_ENDPOINT}/contact/vid/{hubspot_id}/profile'
        return self.update_contact_core(url, property_changes)

    def delete_hubspot_contact(self, id_):
        url = f'{CONTACTS_ENDPOINT}/contact/vid/{id_}'
        return self.delete(url)
    # endregion

    #region Timeline event types
    def list_timeline_event_types(self):
        """
        https://developers.hubspot.com/docs/methods/timeline/get-event-types
        """
        self.set_app_id()
        url = f'{INTEGRATIONS_ENDPOINT}/{self.app_id}/timeline/event-types'
        return self.developer_get(url)


    @staticmethod
    def get_timeline_event_type_id(name: str, correlation_id):
        table_id = get_aws_namespace() + name
        ddb = ddb_utils.Dynamodb()
        item = ddb.get_item('lookups', table_id, correlation_id)
        return item['details']['hubspot_id']

    def set_app_id(self):
        """
        Sets HubSpot app-id of this class instance if that variable is not set yet
        """
        if self.app_id is None:
            self.get_hubspot_connection_secret()

    def get_timeline_event_type_properties(self, tle_type_id):
        url = f'{INTEGRATIONS_ENDPOINT}/{self.app_id}/timeline/event-types/{tle_type_id}/properties'
        result = self.developer_get(url)
        return result

    def create_timeline_event_type(self, type_defn):
        """
        See https://developers.hubspot.com/docs/methods/timeline/create-event-type

        Args:
            type_defn (dict): see test_hubspot.TEST_TLE_TYPE_DEFINITION for an example

        Returns:
            content['id'] (int): ID of created timeline event type
        """
        self.set_app_id()
        type_defn['applicationId'] = self.app_id
        url = f'{INTEGRATIONS_ENDPOINT}/{self.app_id}/timeline/event-types'
        response = self.developer_post(url, type_defn)
        content = json.loads(response.content)
        return content['id']

    def create_timeline_event_type_properties(self, tle_type_id, property_defns: list):
        self.set_app_id()
        url = f'{INTEGRATIONS_ENDPOINT}/{self.app_id}/timeline/event-types/{tle_type_id}/properties'
        results = list()
        for property_defn in property_defns:
            results.append(self.developer_post(url, property_defn))
        return results

    def delete_timeline_event_type_property(self, tle_type_id, property_id):
        """
        See https://developers.hubspot.com/docs/methods/timeline/delete-timeline-event-type-property

        Args:
            tle_type_id: Timeline event type id
            property_id: Property id

        Returns:
            Status code returned by API call
        """
        self.set_app_id()
        url = f'{INTEGRATIONS_ENDPOINT}/{self.app_id}/timeline/event-types/{tle_type_id}/properties/{property_id}'
        result = self.developer_delete(url)
        return result.status_code

    def delete_timeline_event_type(self, tle_type_id):
        """
        See https://developers.hubspot.com/docs/methods/timeline/delete-event-type

        Args:
            tle_type_id: ID of timeline event type to be deleted

        Returns:
            Status code of delete request: Returns a 204 No Content response on success
        """
        self.set_app_id()
        url = f'{INTEGRATIONS_ENDPOINT}/{self.app_id}/timeline/event-types/{tle_type_id}'
        result = self.developer_delete(url)
        return result.status_code
    # endregion

    # region Timeline event instances
    def get_timeline_event(self, tle_type_id, tle_id):
        self.set_app_id()
        url = f'{INTEGRATIONS_ENDPOINT}/{self.app_id}/timeline/event/{tle_type_id}/{tle_id}'
        result = self.get(url)
        return result

    @hubspot_api_error_handler
    def create_or_update_timeline_event(self, event_data: dict):
        self.set_app_id()
        url = f'{INTEGRATIONS_ENDPOINT}/{self.app_id}/timeline/event'
        result = self.put(url, event_data)
        return result.status_code
    # endregion

    # region thiscovery functionality
    def post_new_user_to_crm(self, new_user):
        """

        Args:
            new_user (json): see test_hubspot.TEST_USER_01 for an example

        Returns:
            tuple: (hubspot_id, is_new) if successful, (-1, False) otherwise

        Tested in:
            test_hubspot.test_01_create_contact_ok

        """
        email = new_user['email']

        url = '/contacts/v1/contact/createOrUpdate/email/' + email

        created_timestamp = hubspot_timestamp(new_user['created'])

        data = {
            "properties": [
                {"property": "email", "value": email},
                {"property": "firstname", "value": new_user['first_name']},
                {"property": "lastname", "value": new_user['last_name']},
                {"property": "thiscovery_id", "value": new_user['id']},
                {"property": "thiscovery_registered_date", "value": created_timestamp},
                {"property": "country", "value": new_user['country_name']},
            ]
        }

        result = self.post(url=url, data=data)

        if result.status_code == HTTPStatus.OK:

            content_str = result.content.decode('utf-8')
            content = json.loads(content_str)
            vid = content['vid']
            is_new = content['isNew']
            return vid, is_new

        else:
            return -1, False

    def post_task_signup_to_crm(self, signup_details):
        tle_type_id = self.get_timeline_event_type_id(TASK_SIGNUP_TLE_TYPE_NAME, self.correlation_id)
        tle_details = {
            'id': signup_details['id'],
            'objectId': signup_details['crm_id'],
            'eventTypeId': tle_type_id,
            'project_id': signup_details['project_id'],
            'project_name': signup_details['project_name'],
            'task_id': signup_details['task_id'],
            'task_name': signup_details['task_name'],
            'task_type_id': signup_details['task_type_id'],
            'task_type_name': signup_details['task_type_name'],
            'signup_event_type': signup_details['signup_event_type'],
            'timestamp': hubspot_timestamp(signup_details['created'])
        }

        return self.create_or_update_timeline_event(tle_details)

    def post_user_login_to_crm(self, login_details):
        user_email = login_details['email']
        login_time_str = login_details['login_datetime']
        login_timestamp = hubspot_timestamp(login_time_str)
        property_name = 'thiscovery_last_login_date'
        changes = [
            {"property": property_name, "value": int(login_timestamp)},
        ]
        return self.update_contact_by_email(user_email, changes)
    # endregion


class SingleSendClient(HubSpotClient):
    token_item_id = 'hubspot-emails'
    expired_token_item_id = 'hubspot-emails-expired'
    app_id_secret_name = 'emails-app-id'
    client_id_secret_name = 'emails-client-id'
    client_secret_name = 'emails-client-secret'

    def send_email(self, template_id, message, **kwargs):
        """
        https://legacydocs.hubspot.com/docs/methods/email/transactional_email/single-send-overview

        Args:
            template_id: Id of the rendering template to use
            message (dict): object containing anything that you want to override. At the minimum, the to field must be present.
            **kwargs: see documentation for list of optional params

        Returns:

        """
        data = {
            'emailId': template_id,
            'message': message,
        }
        data.update(**kwargs)
        return self.post(
            url='/email/public/v1/singleEmail/send',
            data=data
        )


# region hubspot timestamp methods
def hubspot_timestamp(datetime_string: str):
    # strip milliseconds and timezone
    datetime_string = datetime_string[:19]
    # date string may contain 'T' - if so then replace with space
    datetime_string = datetime_string.replace('T', ' ')
    datetime_value = datetime.strptime(datetime_string, DATE_FORMAT)
    datetime_timestamp = int(datetime_value.timestamp() * 1000)
    return datetime_timestamp


def hubspot_timestamp_to_datetime(hubspot_timestamp):
    timestamp = int(hubspot_timestamp)/1000
    dt = datetime.fromtimestamp(timestamp, tz=timezone.utc)
    return dt
# endregion
