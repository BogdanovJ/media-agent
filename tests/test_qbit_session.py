from unittest.mock import Mock, patch

import pytest
import requests

from app.qbit import Qbit


def response(code, body=None):
    r = Mock(status_code=code, text="Ok.")
    r.json.return_value = body
    if code >= 400:
        r.raise_for_status.side_effect = requests.HTTPError(str(code))
    return r


@pytest.fixture
def client():
    with patch("app.qbit.requests.Session") as session:
        s = session.return_value
        s.post.return_value = response(200)
        yield Qbit("http://q", "user", "pw"), s


def test_read_reauth_once(client):
    q, s = client
    s.request.side_effect = [response(403), response(200, [])]
    assert q.lookup("a"*40) is None
    assert s.post.call_count == 2
    s.request.side_effect = [response(403), response(403)]
    with pytest.raises(requests.HTTPError):
        q.lookup("a"*40)


def test_no_blind_mutation_retry(client):
    q, s = client
    s.request.return_value = response(403)
    with pytest.raises(requests.HTTPError):
        q.remove_record_only("a"*40, confirmed=True)
    assert s.request.call_count == 1
    assert s.request.call_args.kwargs["data"]["deleteFiles"] == "false"


@pytest.mark.parametrize("body", [{}, [{"hash": "b"*40}], None])
def test_bad_inventory_is_never_absence(client, body):
    q, s = client
    s.request.return_value = response(200, body)
    with pytest.raises(RuntimeError):
        q.lookup("a"*40)
