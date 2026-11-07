import json
import re
from pathlib import Path
import argparse


def main() -> None:
    

    parser = argparse.ArgumentParser()


    parser.add_argument(
        "--runtime-evidence",
        default="runtime_failure.json",
        help="Runtime evidence JSON file",
    )

    parser.add_argument(
        "--git-evidence",
        default="git_evidence.json",
        help="Git evidence JSON file",
    )

    args = parser.parse_args()

    runtime_evidence = json.loads(
        Path(args.runtime_evidence).read_text(
            encoding="utf-8"
        )
    )

    git_evidence = json.loads(
        Path(args.git_evidence).read_text(
            encoding="utf-8"
        )
    )

    hypothesis_data = json.loads(
        Path("hypothesis.json").read_text(
            encoding="utf-8"
        )
    )

    hypothesis = hypothesis_data["hypothesis"]
    checks = []

    command_failed = runtime_evidence["exit_code"] != 0
    checks.append(
        {
            "check": "Reproduction command failed",
            "passed": command_failed,
            "details": (
                f"Exit code: "
                f"{runtime_evidence['exit_code']}"
            ),
        }
    )

    stack_locations = re.findall(
        r"\(([^()\s]+):(\d+):(\d+)\)",
        runtime_evidence["stderr"],
    )

    if stack_locations:
        failing_path, line, column = stack_locations[0]
        failing_file = Path(failing_path)

        file_exists = failing_file.exists()
        checks.append(
            {
                "check": "Runtime-reported file exists",
                "passed": file_exists,
                "details": str(failing_file),
            }
        )

        reported_location = (
            failing_file.name in hypothesis
            and re.search(
                rf"\bline\b[\s:`-]*{re.escape(line)}\b",
                hypothesis,
                flags=re.IGNORECASE,
            )
            is not None
        )

        checks.append(
            {
                "check": "Hypothesis mentions runtime location",
                "passed": reported_location,
                "details": (
                    f"{failing_file.name}:{line}:{column}"
                ),
            }
        )

        if file_exists:
            source_lines = failing_file.read_text(
                encoding="utf-8"
            ).splitlines()

            source_line = source_lines[int(line) - 1]

            line_contains_field = (
                "discountType" in source_line
            )

            checks.append(
                {
                    "check": "Failing source line contains discountType",
                    "passed": line_contains_field,
                    "details": source_line.strip(),
                }
            )

    commit_match = re.search(
        r"commit ([0-9a-f]{7,40})",
        git_evidence["commit_summary"],
    )

    if commit_match:
        commit_hash = commit_match.group(1)
        short_hash = commit_hash[:8]

        commit_mentioned = short_hash in hypothesis

        checks.append(
            {
                "check": "Hypothesis mentions introducing commit",
                "passed": commit_mentioned,
                "details": short_hash,
            }
        )

    print("Validation results")
    print("=" * 80)

    all_passed = True

    for check in checks:
        status = "PASS" if check["passed"] else "FAIL"

        print(
            f"[{status}] {check['check']}: "
            f"{check['details']}"
        )

        if not check["passed"]:
            all_passed = False

    print()
    print(
        "Overall result: "
        + ("VALIDATED" if all_passed else "NEEDS REVIEW")
    )


if __name__ == "__main__":
    main()
