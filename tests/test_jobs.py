from unittest.mock import Mock

import yaml

from app import jobs


def test_submission_quota_duplicates_and_isolation(tmp_path, monkeypatch):
    (tmp_path / "token").write_text("test")
    (tmp_path / "namespace").write_text("media-agent")
    template = tmp_path / "template"
    container = dict(name="worker", volumeMounts=[dict(name="downloads", mountPath="/work")])
    template.write_text(yaml.safe_dump(dict(metadata={}, spec=dict(template=dict(spec=dict(containers=[container]))))))
    monkeypatch.setattr(jobs, "SERVICE_ACCOUNT", tmp_path)
    monkeypatch.setattr(jobs, "TEMPLATE", template)
    monkeypatch.setenv("WORKER_IMAGE", "test@digest")
    get = Mock()
    post = Mock()
    monkeypatch.setattr(jobs.requests, "get", get)
    monkeypatch.setattr(jobs.requests, "post", post)
    src = tmp_path / "file.MP4"
    directory = tmp_path / ".media-agent" / ("a"*40) / ("b"*64)
    def submit(active):
        get.return_value.json.return_value = {"items": [dict(metadata=dict(name=n)) for n in active]}
        jobs.submit(tmp_path, directory, src, "remux", None)
    submit(["a", "b"])
    post.assert_not_called()
    submit([])
    job = post.call_args.kwargs["json"]
    c = job["spec"]["template"]["spec"]["containers"][0]
    assert c["args"] == [str(src), "remux"]
    assert c["volumeMounts"][0]["subPath"] == str(directory.relative_to(tmp_path))
    submit([job["metadata"]["name"]])
    assert post.call_count == 1
