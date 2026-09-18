from requests import sessions as request_sessions


def make_session():
    # ruleid: no-requests-outside-http-client
    return request_sessions.Session()
