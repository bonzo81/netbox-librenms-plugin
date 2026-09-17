from requests import get


def fetch(url):
    # ruleid: no-requests-outside-http-client
    return get(url, timeout=5)
