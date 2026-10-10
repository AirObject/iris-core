"""Reuse the frozen-request comparator, including all five public corpora (116 cases)."""
from pathlib import Path
import sys

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
import compare_learning_requests as comparison

comparison.CORPORA = (*comparison.CORPORA, 'learning_v5.jsonl')
# The original orchestrator starts this entry point again for isolated workers.
comparison.__file__ = __file__

if __name__ == '__main__':
    comparison.main()
