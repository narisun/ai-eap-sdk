from pathlib import Path

import pytest
from eap_cli.scaffolders.render import render_template_dir


def test_render_template_dir_writes_files_and_strips_j2(tmp_path: Path):
    src = tmp_path / "src"
    src.mkdir()
    (src / "template.toml").write_text('[template]\nname = "demo"\n')
    (src / "hello.txt.j2").write_text("Hello {{ name }}!")
    (src / "sub").mkdir()
    (src / "sub" / "nested.py.j2").write_text("# project={{ name }}\n")

    dst = tmp_path / "dst"
    render_template_dir(src, dst, {"name": "world"})

    assert (dst / "hello.txt").read_text() == "Hello world!"
    assert (dst / "sub" / "nested.py").read_text() == "# project=world\n"
    # template.toml is metadata, not rendered into the output
    assert not (dst / "template.toml").exists()


def test_render_refuses_to_overwrite_unless_force(tmp_path: Path):
    src = tmp_path / "src"
    src.mkdir()
    (src / "x.txt.j2").write_text("v1")
    dst = tmp_path / "dst"
    render_template_dir(src, dst, {})

    (dst / "x.txt").write_text("user-modified")
    with pytest.raises(FileExistsError):
        render_template_dir(src, dst, {})

    render_template_dir(src, dst, {}, force=True)
    assert (dst / "x.txt").read_text() == "v1"


def test_render_substitutes_name_in_filenames(tmp_path: Path):
    src = tmp_path / "src"
    (src / "tools").mkdir(parents=True)
    (src / "tools" / "__name__.py.j2").write_text("# tool {{ name }}\n")
    dst = tmp_path / "dst"
    render_template_dir(src, dst, {"name": "lookup_user"})
    assert (dst / "tools" / "lookup_user.py").read_text() == "# tool lookup_user\n"


def test_maybe_substitute_name_no_name_key(tmp_path: Path):
    """_maybe_substitute_name is a no-op when 'name' not in variables."""
    from eap_cli.scaffolders.render import _maybe_substitute_name

    p = Path("tools/__name__.py")
    result = _maybe_substitute_name(p, {})
    assert result == p


def test_maybe_substitute_name_directory_segment(tmp_path: Path):
    """__name__ appearing as a directory segment is also substituted."""
    from eap_cli.scaffolders.render import _maybe_substitute_name

    p = Path("__name__/foo.py")
    result = _maybe_substitute_name(p, {"name": "mymod"})
    assert result == Path("mymod/foo.py")


def test_render_copies_non_j2_files_verbatim(tmp_path: Path):
    src = tmp_path / "src"
    src.mkdir()
    (src / "data.json").write_text('{"key": "value"}')
    dst = tmp_path / "dst"
    render_template_dir(src, dst, {})
    assert (dst / "data.json").read_text() == '{"key": "value"}'
