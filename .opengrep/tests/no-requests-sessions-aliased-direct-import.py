from requests.sessions import Session as RequestsSession


def make_session():
    # ruleid: no-requests-outside-http-client
    return RequestsSession()
