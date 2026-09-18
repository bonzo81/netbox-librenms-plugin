import requests.sessions


def make_session():
    # ruleid: no-requests-outside-http-client
    return requests.sessions.Session()
