import requests
import requests as http
from requests import get as fetch


def direct_calls(url):
    # ruleid: no-requests-outside-http-client
    requests.get(url)
    # ruleid: no-requests-outside-http-client
    requests.post(url)
    # ruleid: no-requests-outside-http-client
    requests.put(url)
    # ruleid: no-requests-outside-http-client
    requests.patch(url)
    # ruleid: no-requests-outside-http-client
    requests.delete(url)
    # ruleid: no-requests-outside-http-client
    requests.request("GET", url)
    # ruleid: no-requests-outside-http-client
    requests.Session()
    # ruleid: no-requests-outside-http-client
    http.get(url)
    # ruleid: no-requests-outside-http-client
    fetch(url)


def shared_client(client):
    # ok: no-requests-outside-http-client
    client.get_device_info(42)
    # ok: no-requests-outside-http-client
    requests.exceptions.RequestException("failed")



def lookup(requests, key):
    # ok: no-requests-outside-http-client
    return requests.get(key)


def local_lookup(key):
    requests = {"port": 42}
    # ok: no-requests-outside-http-client
    return requests.get(key)



def more_calls(url):
    from requests import get
    from requests.api import get as api_get
    import requests.api as api
    # ruleid: no-requests-outside-http-client
    get(url)
    # ruleid: no-requests-outside-http-client
    requests.head(url)
    # ruleid: no-requests-outside-http-client
    requests.options(url)
    # ruleid: no-requests-outside-http-client
    requests.session()
    # ruleid: no-requests-outside-http-client
    api_get(url)
    # ruleid: no-requests-outside-http-client
    api.get(url)


def shadow_alias(http, fetch, key):
    # ok: no-requests-outside-http-client
    http.get(key)
    # ok: no-requests-outside-http-client
    fetch(key)



def api_import(url):
    from requests.api import get
    # ruleid: no-requests-outside-http-client
    return get(url)
