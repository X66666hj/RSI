"""Check the existing split and print the expansion experiment's offline run sheet."""
import argparse
import hashlib
import json
from pathlib import Path

ROOT = Path(__file__).resolve().parent
FROZEN = ROOT / "frozen-runtime"
TRAIN_VERSIONS = {
    "polyglot-c-py": (1, 2, 3, 4),
    "cobol-modernization": (1, 2, 3, 4),
    "code-from-image": (1, 2, 3, 4),
}
TEST_VERSIONS = {
    "circuit-fibsqrt": (1, 2, 3, 4),
    "git-leak-recovery": (1, 2, 3, 4),
}


def digest(path):
    with path.open("rb") as stream:
        return hashlib.file_digest(stream, "sha256").hexdigest()


def paired_training_plan(accepted, train_versions=TRAIN_VERSIONS):
    """Keep every original stage and pair only the admitted extra practice."""
    if not train_versions or any(tuple(versions) != (1, 2, 3, 4) for versions in train_versions.values()):
        raise ValueError("Train families require complete EVO-1 through EVO-4 chains")
    expected = {f"{family}-EVO-{v}" for family, versions in train_versions.items() for v in versions}
    if not isinstance(accepted, dict) or set(accepted) - expected:
        raise ValueError("Accepted variants must belong to selected Train stages")
    allowed = {f"variant-{n:02}" for n in range(1, 5)}
    chains, pairs = [], []
    for family, versions in train_versions.items():
        stages = []
        for version in versions:
            parent = f"{family}-EVO-{version}"
            ids = accepted.get(parent, [])
            if (not isinstance(ids, list) or len(ids) > 4
                    or any(not isinstance(variant, str) or variant not in allowed for variant in ids)
                    or len(set(ids)) != len(ids)):
                raise ValueError(f"{parent} must have 0-4 distinct admitted variant IDs")
            variants = sorted(ids)
            stages.append({"parent": parent, "variants": variants})
            pairs.extend({"parent": parent, "variant": variant, "B_repeats": 1, "C_trials": 1}
                         for variant in variants)
        chains.append({"family": family, "stages": stages})
    return chains, pairs


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--accepted", type=Path,
                        help="Accepted variant IDs by Train parent. Omitted parents run their original only.")
    args = parser.parse_args()
    manifest = FROZEN / "expanded-plan.json"
    plan = json.loads(manifest.read_text(encoding="utf-8"))
    roles = json.loads((ROOT / "envharness_qwen_roles.json").read_text(encoding="utf-8"))
    protocol = json.loads((ROOT / "envharness_evo_chain_protocol.json").read_text(encoding="utf-8"))
    if (protocol["train_versions"] != {c: list(v) for c, v in TRAIN_VERSIONS.items()}
            or protocol["test_versions"] != {c: list(v) for c, v in TEST_VERSIONS.items()}
            or list(TRAIN_VERSIONS) != plan["train_chains"][:3]
            or list(TEST_VERSIONS) != plan["test_chains"][:2]):
        raise ValueError("Chain selection differs from the frozen protocol")
    train = [task for block in plan["blocks"] for task in block["tasks"]]
    test = [f"{chain}-EVO-{version}" for chain in plan["test_chains"] for version in range(1, 5)]
    expected_train = {f"{chain}-EVO-{v}" for chain in plan["train_chains"] for v in range(1, 5)}
    if (len(train) != 24 or set(train) != expected_train or len(set(test)) != 12
            or set(plan["train_chains"]) & set(plan["test_chains"])
            or set(train + test) != set(plan["tasks"])):
        raise ValueError("Frozen split or task coverage mismatch")
    if digest(manifest) != "6cd18dddb43fa37367c2ca9fc9e70ea189f03c62b0c6d3ca9aff512b16c3d4ee":
        raise ValueError("Unexpected source manifest")
    batch_train_ids = {f"{chain}-EVO-{v}" for chain, versions in TRAIN_VERSIONS.items() for v in versions}
    batch_test_ids = {f"{chain}-EVO-{v}" for chain, versions in TEST_VERSIONS.items() for v in versions}
    train = [task for task in train if task in batch_train_ids]
    test = [task for task in test if task in batch_test_ids]
    if (len(train) != 12 or len(test) != 8
            or any(versions != (1, 2, 3, 4) for versions in [*TRAIN_VERSIONS.values(), *TEST_VERSIONS.values()])):
        raise ValueError("Require three complete Train chains and two complete Test chains")
    roots = [ROOT / "source", ROOT / "skill_evo_expanded_screen_20261019/screen-source",
             ROOT / "skill_evo_category_expansion_20261018/review-source"]
    verified, missing = {}, []
    for task in train + test:
        expected = {k.split("/", 1)[1]: h for k, h in plan["source_sha256"].items()
                    if k.startswith(task + "/")}
        for root in roots:
            directory = root / task
            if not directory.is_dir() or not expected:
                continue
            actual = {p.relative_to(directory).as_posix(): digest(p)
                      for p in directory.rglob("*") if p.is_file()}
            if actual == expected:
                verified[task] = str(directory.relative_to(ROOT))
                break
        else:
            missing.append(task)
    if missing:
        raise ValueError(f"Unverified first-batch task trees: {missing}")
    accepted = json.loads(args.accepted.read_text(encoding="utf-8")) if args.accepted else {}
    chains, pairs = paired_training_plan(accepted)
    print(json.dumps({
        "mode": "offline_preflight", "model_calls": 0, "split_sha256": digest(manifest),
        "solver": roles["roles"]["solver"], "train": train, "held_out": test,
        "protocol": "complete-evo-chains-v2", "train_chains": list(TRAIN_VERSIONS),
        "test_chains": list(TEST_VERSIONS), "skill_inheritance": "stage-before to stage-after to next EVO",
        "matching_local_task_trees": verified, "unverified_task_trees": missing,
        "initial_skill_sha256": {n: digest(FROZEN / n) for n in ("FORMAT_S0.md", "TASK_S0.md")},
        "min_accepted_per_parent": 0, "max_variants_per_parent": 4,
        "max_variants_batch": 4 * len(train), "reuse_solver_results": False,
        "paired_extra_practice": pairs, "expansion_manifest_supplied": args.accepted is not None,
        "train_stage_plan": chains,
        "effective_train_positions_per_group": len(train) + len(pairs),
        "S0_baseline_train": len(train), "shared_stage_one_train": len(TRAIN_VERSIONS),
        "later_group_specific_original_trials": 2 * 3 * len(TRAIN_VERSIONS),
        "solver_positions_without_extra_practice": len(train) + 2 * 3 * len(TRAIN_VERSIONS) + 3 * len(test),
        "B_parent_repeats_per_variant": 1,
        "C_variant_trials_per_variant": 1, "ABC_test_attempts": 3 * len(test),
    }, indent=2))


if __name__ == "__main__":
    main()
