import requests

from loom import urls


def test_urls():
    url_attributes = [
        attr
        for attr in dir(urls)
        if not callable(getattr(urls, attr)) and not attr.startswith("__")
    ]
    for attr in url_attributes:
        url = getattr(urls, attr)
        if not url:  # no loom docs page for this yet
            continue
        if url.startswith(urls.docs):  # checked offline against loom/docs by test_docs.py
            continue
        response = requests.get(url)
        assert response.status_code == 200, f"URL {url} returned status code {response.status_code}"
