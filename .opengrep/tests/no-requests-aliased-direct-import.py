from requests import get as fetch_it


def fetch(url):
    # ruleid: no-requests-outside-http-client
    return fetch_it(url, timeout=5)
