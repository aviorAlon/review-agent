from review_agent.diff import parse_diff


def test_parse_sample_diff(sample_diff):
    hunks = parse_diff(sample_diff)
    assert len(hunks) == 1
    h = hunks[0]
    assert h.file == "app/auth.py"
    assert h.start_line == 1
    assert h.added_line_numbers == [2, 3, 4, 5, 6, 7]
    assert any("is_public" in line for line in h.added_lines)
    assert any("is_admin" in line for line in h.removed_lines)
    # unchanged lines are kept with their target-side numbers
    assert h.context_line_numbers == [1, 8, 9, 10, 11]
    assert h.context_lines[0] == "def check_permission(user, resource):"
    assert h.context_lines[-1] == "    return db.get(user_id)"
    # removed lines remember the target line they followed (all three followed line 1)
    assert h.removed_anchors == [1, 1, 1]


def test_removed_anchor_is_zero_before_first_line():
    diff = """--- a/x.py
+++ b/x.py
@@ -1,2 +1,1 @@
-gone = 1
 kept = 2
"""
    h = parse_diff(diff)[0]
    assert h.removed_lines == ["gone = 1"] and h.removed_anchors == [0]
    assert h.context_line_numbers == [1]


def test_parse_skips_removed_files():
    diff = """diff --git a/gone.py b/gone.py
deleted file mode 100644
--- a/gone.py
+++ /dev/null
@@ -1,2 +0,0 @@
-x = 1
-y = 2
"""
    assert parse_diff(diff) == []
