"""一次性预检：mutate.py 里每条变形的 old 串，在目标文件里是否**恰好出现一次**。

跑变异之前先静态核对，省掉"跑 12 分钟后判『工具错』"。
"""
import ast
import pathlib

HERE = pathlib.Path(__file__).resolve().parent          # _verify/
ROOT = HERE.parent                                       # 仓库根
tree = ast.parse((HERE / "mutate.py").read_text(encoding="utf-8"))

CONSTS = {}
for node in tree.body:
    if isinstance(node, ast.Assign) and isinstance(node.targets[0], ast.Name):
        name = node.targets[0].id
        val = node.value
        if isinstance(val, ast.BinOp) and isinstance(val.right, ast.Constant):
            CONSTS[name] = pathlib.PurePosixPath(val.right.value)

bad = 0
checked = 0
for node in ast.walk(tree):
    if not isinstance(node, ast.Tuple) or len(node.elts) != 5:
        continue
    title = node.elts[0]
    if not (isinstance(title, ast.Constant) and isinstance(title.value, str)):
        continue
    t = title.value
    if not (t.startswith("s1") or t.startswith("s2")):
        continue
    fnode, old, new = node.elts[1], node.elts[2], node.elts[3]
    if not (isinstance(fnode, ast.Name) and isinstance(old, ast.Constant)
            and isinstance(new, ast.Constant)):
        continue
    rel = CONSTS.get(fnode.id)
    if rel is None:
        continue
    path = ROOT / str(rel).replace("/", __import__("os").sep)
    if not path.exists():
        print(f"缺失文件 {t[:28]} -> {rel}")
        bad += 1
        continue
    src = path.read_text(encoding="utf-8")
    n = src.count(old.value)
    checked += 1
    if n != 1:
        print(f"BAD({n}) {t[:40]} -> {rel}")
        bad += 1

print(f"预检 {checked} 条变形锚点，{bad} 条不唯一/找不到")
