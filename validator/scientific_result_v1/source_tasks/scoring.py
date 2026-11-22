"""Exact hierarchical score computation; unresolved evidence is never a zero."""

from collections import defaultdict
from fractions import Fraction
from .contracts import FractionValue, PaperScoreVector


def value(x):
    return (
        None
        if x is None
        else FractionValue(numerator=x.numerator, denominator=x.denominator)
    )


def mean(values):
    values = list(values)
    if not values:
        raise ValueError("Empty measured population")
    return sum(values, Fraction()) / len(values)


def quality(*, gate, content, archive, audit, content_available, archive_available):
    if not content_available and not archive_available:
        return None
    if gate == 0:
        return Fraction(0)
    if (
        gate is None
        or audit is None
        or (content_available and content is None)
        or (archive_available and archive is None)
    ):
        return None
    return (
        Fraction(audit)
        * (7 * Fraction(content or 0) + 2 * Fraction(archive or 0))
        / (7 * content_available + 2 * archive_available)
    )


def score(binding, alias, gate, source_frame, selected, bits, *, hold=False):
    """bits are controller TaskBits; completeness checked against selected task manifests upstream."""
    aC = any(t.eligible_content for t in source_frame.tiles)
    aT = any(t.numerical for t in source_frame.tables)
    base = dict(
        binding=binding,
        output_alias=alias,
        gate=gate,
        content_available=aC,
        archive_available=aT,
        content_prose=None,
        content_table=None,
        content=None,
        archive=None,
        output_audit=None,
        score=None,
        task_bits=bits,
        skipped_reason=None,
    )
    if not aC and not aT:
        return PaperScoreVector(state="INELIGIBLE", **base)
    if hold or gate is None:
        return PaperScoreVector(state="HOLD", **base)
    if gate == 0:
        return PaperScoreVector(
            state="PIPELINE_FAILURE",
            **{
                **base,
                "score": value(Fraction(0)),
                "skipped_reason": "GATE_FAILED",
                "task_bits": [],
            },
        )
    task_ids = [bit.task_id for bit in bits]
    if len(task_ids) != len(set(task_ids)):
        raise ValueError("Duplicate scientific task bit")
    expected = {("CONTENT", key) for key in selected["content_prose"] + selected["content_table"]}
    expected |= {("ARCHIVE", key) for key in selected["archive"]}
    expected |= {("OUTPUT_AUDIT", key) for key in selected.get("output_audit", [])}
    actual = {(bit.task_kind, bit.tile_or_bucket_id) for bit in bits}
    if actual != expected:
        raise ValueError("Scientific bits do not cover exactly the selected tasks/buckets")
    from .sharding import parent_id
    joint=defaultdict(list)
    for bit in bits:
        joint[(bit.task_kind,bit.tile_or_bucket_id,parent_id(bit.task_id))].append(Fraction(bit.bit))
    by_key = defaultdict(list)
    for (kind,key,_),values in joint.items():
        by_key[(kind,key)].append(min(values))
    tile_map = {t.tile_id: t for t in source_frame.tiles}

    def tile_mean(ids, kind, hierarchical):
        groups = defaultdict(list)
        for key in ids:
            vals = by_key[(kind, key)]
            if not vals:
                raise ValueError("Missing task bit")
            groups[tile_map[key].table_uid if hierarchical else key].append(mean(vals))
        return mean(mean(v) for v in groups.values()) if groups else None

    cp = tile_mean(selected["content_prose"], "CONTENT", False)
    ct = tile_mean(selected["content_table"], "CONTENT", True)
    c = mean(x for x in (cp, ct) if x is not None) if aC else None
    t = tile_mean(selected["archive"], "ARCHIVE", True)
    pkeys = selected.get("output_audit", [])
    pb = {k: min(by_key[("OUTPUT_AUDIT", k)]) for k in pkeys}
    p = (
        Fraction(0)
        if pb.get("UNANCHORED") == 0
        else mean(v for k, v in pb.items() if k != "UNANCHORED")
        if any(k != "UNANCHORED" for k in pb)
        else Fraction(1)
    )
    q = quality(
        gate=gate,
        content=c,
        archive=t,
        audit=p,
        content_available=aC,
        archive_available=aT,
    )
    return PaperScoreVector(
        state="SCORED",
        **{
            **base,
            "content_prose": value(cp),
            "content_table": value(ct),
            "content": value(c),
            "archive": value(t),
            "output_audit": value(p),
            "score": value(q),
        },
    )
