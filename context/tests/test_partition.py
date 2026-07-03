from context.partition import normalize_remote, partition_from, resolve_partition


def test_normalize_scp_like():
    assert normalize_remote("git@github.com:org/repo.git") == "github.com/org/repo"


def test_normalize_https_strips_git():
    assert normalize_remote("https://github.com/Org/Repo.git") == "github.com/org/repo"


def test_normalize_ssh_with_port():
    assert normalize_remote("ssh://git@host.example:2222/team/sub/proj.git") == "host.example/team/sub/proj"


def test_normalize_nested_path():
    assert normalize_remote("https://gitlab.com/g/sub/repo") == "gitlab.com/g/sub/repo"


def test_normalize_rejects_garbage():
    assert normalize_remote("") is None
    assert normalize_remote("not a url") is None


def test_partition_prefers_origin():
    assert partition_from("git@github.com:o/r.git", "/home/x/whatever", "/tmp/z") == "github.com/o/r"


def test_partition_falls_back_to_toplevel():
    assert partition_from(None, "/home/x/code/Divide-And-Conquer", "/tmp/z") == "divide-and-conquer"


def test_partition_falls_back_to_cwd():
    assert partition_from(None, None, "/home/x/code/myproj") == "myproj"


def test_partition_bad_origin_falls_through():
    # unparseable origin -> use toplevel, not a broken key
    assert partition_from("garbage", "/srv/acme", "/tmp/z") == "acme"


def test_resolve_partition_explicit_wins_and_lowercases():
    # explicit key short-circuits detection (no git/fs probe) and normalizes case
    assert resolve_partition("GitHub.com/Org/Repo", cwd="/anywhere") == "github.com/org/repo"
