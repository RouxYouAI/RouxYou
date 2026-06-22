"""Unit tests for the path sanitizer helpers in coder/coder.py.

These tests validate the user-constraint path defense added Apr 6, 2026
in response to the verifier_test.txt regression where GPT-OSS 20B invented
an entirely different path than the one the user requested.

Run from project root:
    python -m pytest tests/test_path_sanitizer.py -v
or:
    python tests/test_path_sanitizer.py
"""
import os
import sys
import importlib.util

# coder/ is not a package (no __init__.py). Load coder.py by absolute path
# and grab just the helpers we want — this avoids dragging in the FastAPI app.
HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(HERE)
sys.path.insert(0, ROOT)

CODER_PATH = os.path.join(ROOT, "coder", "coder.py")
_spec = importlib.util.spec_from_file_location("coder_module", CODER_PATH)
_coder = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(_coder)

_extract_user_paths = _coder._extract_user_paths
_normalize_path_for_compare = _coder._normalize_path_for_compare


def _check(label, actual, expected):
    ok = actual == expected
    marker = "PASS" if ok else "FAIL"
    print(f"  [{marker}] {label}")
    if not ok:
        print(f"         expected: {expected!r}")
        print(f"         actual:   {actual!r}")
    return ok


def test_extract_basic_forward_slash():
    print("test_extract_basic_forward_slash")
    paths = _extract_user_paths("create a file at C:/Users/testuser/Desktop/verifier_test.txt that contains hello")
    return _check("forward-slash extraction", paths, ["C:/Users/testuser/Desktop/verifier_test.txt"])


def test_extract_basic_backslash():
    print("test_extract_basic_backslash")
    paths = _extract_user_paths(r"read C:\Users\testuser\Desktop\notes.md and tell me what is in it")
    return _check("backslash extraction", paths, [r"C:\Users\testuser\Desktop\notes.md"])


def test_extract_over_escaped():
    print("test_extract_over_escaped")
    paths = _extract_user_paths(r"write to C:\\Users\\testuser\\Desktop\\foo.txt please")
    return _check("over-escaped collapse", paths, [r"C:\Users\testuser\Desktop\foo.txt"])


def test_extract_with_trailing_punctuation():
    print("test_extract_with_trailing_punctuation")
    paths = _extract_user_paths("delete C:/Users/testuser/Desktop/temp.log.")
    return _check("trailing period stripped", paths, ["C:/Users/testuser/Desktop/temp.log"])


def test_extract_multiple_paths():
    print("test_extract_multiple_paths")
    paths = _extract_user_paths("read C:/Desktop/A.py and write the output to C:/Desktop/B.py")
    return _check("multi-path extraction", paths, ["C:/Desktop/A.py", "C:/Desktop/B.py"])


def test_extract_no_paths():
    print("test_extract_no_paths")
    paths = _extract_user_paths("just tell me a joke about lobsters")
    return _check("no paths in chat", paths, [])


def test_extract_empty():
    print("test_extract_empty")
    paths = _extract_user_paths("")
    return _check("empty input", paths, [])


def test_normalize_forward_to_back():
    print("test_normalize_forward_to_back")
    n = _normalize_path_for_compare("C:/Users/testuser/Desktop/foo.txt")
    return _check("forward to back + lower", n, r"c:\users\testuser\desktop\foo.txt")


def test_normalize_case_insensitive():
    print("test_normalize_case_insensitive")
    a = _normalize_path_for_compare(r"C:\Users\TestUser\Desktop\Foo.TXT")
    b = _normalize_path_for_compare("c:/users/testuser/desktop/foo.txt")
    return _check("case insensitive equality", a, b)


def test_normalize_trailing_slash():
    print("test_normalize_trailing_slash")
    n = _normalize_path_for_compare(r"C:\Users\testuser\Desktop\\")
    return _check("trailing slash stripped", n, r"c:\users\testuser\desktop")


def test_apr6_regression_scenario():
    """The exact scenario the verifier caught on Apr 6 morning.

    User said: 'create a file at C:/Users/testuser/Desktop/verifier_test.txt that contains the word hello'
    GPT-OSS 20B chose: C:\\Users\\testuser\\Desktop\\SelfModifyingAgents\\hello

    The new defense should:
    1. Extract the user's path from the task
    2. Normalize both paths and detect they don't match
    3. Trigger override (verified at integration time, not here)
    """
    print("test_apr6_regression_scenario")
    user_text = "create a file at C:/Users/testuser/Desktop/verifier_test.txt that contains the word hello"
    user_paths = _extract_user_paths(user_text)
    if not _check("extracted user path", user_paths, ["C:/Users/testuser/Desktop/verifier_test.txt"]):
        return False

    model_chose = r"C:\Users\testuser\Desktop\SelfModifyingAgents\hello"
    model_norm = _normalize_path_for_compare(model_chose)
    user_norm = [_normalize_path_for_compare(p) for p in user_paths]
    return _check("model path NOT in user paths (mismatch detected)", model_norm not in user_norm, True)


def test_legitimate_match_passes_through():
    """When the model uses the user's exact path, no override should fire."""
    print("test_legitimate_match_passes_through")
    user_text = "write to C:/Users/testuser/Desktop/foo.txt"
    user_paths = _extract_user_paths(user_text)
    model_chose = r"C:\Users\testuser\Desktop\foo.txt"
    model_norm = _normalize_path_for_compare(model_chose)
    user_norm = [_normalize_path_for_compare(p) for p in user_paths]
    return _check("model path matches after normalization", model_norm in user_norm, True)


if __name__ == "__main__":
    tests = [
        test_extract_basic_forward_slash,
        test_extract_basic_backslash,
        test_extract_over_escaped,
        test_extract_with_trailing_punctuation,
        test_extract_multiple_paths,
        test_extract_no_paths,
        test_extract_empty,
        test_normalize_forward_to_back,
        test_normalize_case_insensitive,
        test_normalize_trailing_slash,
        test_apr6_regression_scenario,
        test_legitimate_match_passes_through,
    ]
    results = [t() for t in tests]
    passed = sum(1 for r in results if r)
    total = len(results)
    print(f"\n{passed}/{total} tests passed")
    sys.exit(0 if passed == total else 1)
