from requests import sessions


def make_session():
    # ruleid: no-requests-outside-http-client
    return sessions.Session()
