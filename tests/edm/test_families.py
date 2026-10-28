from service_control_ioc.config.load import load_config_text
from service_control_ioc.config.spec import StackSpec
from service_control_ioc.edm.families import (
    edge_rows,
    instance_columns,
    service_families,
)

_COLLIDING = """
prefix: P
top: a
services:
  a: {unit: a.service, pv: A_B}
  b: {unit: b.service, pv: a_b}
"""

_SPARSE = """
prefix: P
top: a
services: {a: {unit: "a@{n}.service", pv: "A{n}", instances: [3, 5]}}
"""

_FILES = """
prefix: P
top: a
services:
  a:
    unit: "a@{n}.service"
    pv: "A{n}"
    instances: [1, 2]
    listeners: {http: {proto: tcp, port: "8000 + n"}}
    files:
      - {path: "/etc/a-{n}.conf"}
      - {path: /etc/b.conf}
"""


def test_xspress_columns(xspress: StackSpec) -> None:
    assert instance_columns(xspress) == (1, 2, 3, 4)


def test_xspress_families(xspress: StackSpec) -> None:
    fams = service_families(xspress)
    assert [f.key for f in fams] == [
        "frame_receiver",
        "frame_processor",
        "xspress_control",
        "meta_writer",
        "live_view_merge",
        "odin_control",
        "xspress_ioc",
    ]
    fr = fams[0]
    assert fr.members == {n: f"frame_receiver@{n}" for n in (1, 2, 3, 4)}
    assert [r.label for r in fr.rows] == ["control", "frame_ready", "frame_release"]
    assert fr.rows[0].cells[3] == "frame_receiver@3.control"
    xc = fams[2]
    assert xc.members == {None: "xspress_control"}
    assert xc.rows[0].cells == {None: "xspress_control.control"}
    assert xc.rows[1].cells == {n: f"xspress_control.daq@{n}" for n in (1, 2, 3, 4)}


def test_xspress_edge_rows(xspress: StackSpec) -> None:
    rows = edge_rows(xspress)
    assert len(rows) == 14
    assert rows[0].label == "odin_control -> frame_receiver.control"
    assert rows[0].cells[2] == "odin_control -> frame_receiver@2.control"
    assert rows[6].cells == {None: "odin_control -> xspress_control.control"}
    assert [r.post for r in rows] == [False] * 10 + [True] * 4
    assert rows[10].label == "xspress_control -> card.p30124"
    assert sum(len(r.cells) for r in rows) == 44


def test_rows_without_hardware(xspress_text: str) -> None:
    spec = load_config_text(xspress_text.replace("hardware: true", "hardware: false"))
    rows = edge_rows(spec)
    assert len(rows) == 10
    assert [r.post for r in rows] == [False] * 10
    xc = next(f for f in service_families(spec) if f.key == "xspress_control")
    assert [r.label for r in xc.rows] == ["control"]


def test_file_rows() -> None:
    (fam,) = service_families(load_config_text(_FILES))
    assert [r.label for r in fam.rows] == ["http", "file0", "file1"]
    assert fam.rows[1].cells == {1: "a@1:file0", 2: "a@2:file0"}


def test_sparse_instances() -> None:
    spec = load_config_text(_SPARSE)
    assert instance_columns(spec) == (3, 5)
    (fam,) = service_families(spec)
    assert fam.members == {3: "a@3", 5: "a@5"}


def test_no_instances() -> None:
    spec = load_config_text(_COLLIDING)
    assert instance_columns(spec) == ()
    fams = service_families(spec)
    assert [f.key for f in fams] == ["a", "b"]
    assert all(set(f.members) == {None} for f in fams)
