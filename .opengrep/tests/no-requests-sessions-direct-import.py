from requests.sessions import Session


def make_session():
    # ruleid: no-requests-outside-http-client
    return Session()
