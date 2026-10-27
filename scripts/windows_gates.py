"""Require each CI gate to finish successfully; missing/skipped gates fail."""
import argparse
import json
import os
from pathlib import Path


def evaluate_gates(steps, required):
    outcomes = {name: steps.get(name, {}).get('outcome', 'missing') for name in required}
    return dict(success=bool(required) and all(outcome == 'success' for outcome in outcomes.values()), gates=outcomes)


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument('--output', required=True, type=Path)
    parser.add_argument('required', nargs='+')
    args = parser.parse_args()
    report = evaluate_gates(json.loads(os.environ['GATE_OUTCOMES']), args.required)
    args.output.parent.mkdir(parents=True, exist_ok=True)
    args.output.write_text(json.dumps(report, indent=2)+'\n', encoding='utf-8')
    print(json.dumps(report, indent=2))
    return 0 if report['success'] else 1


if __name__ == '__main__':
    raise SystemExit(main())
