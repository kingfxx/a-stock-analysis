"""User-managed groups and browsing history, independent of source facts."""

from pypinyin import lazy_pinyin, Style

from .storage import utc_now
from .update_service import backup_before_update


class StockLibrary:
    def __init__(self, db):
        self.db = db

    def read(self):
        with self.db.connection() as conn:
            stocks = [dict(row) for row in conn.execute(
                "SELECT code,name FROM instruments WHERE id NOT IN (SELECT instrument_id FROM stock_unfollowed) ORDER BY code")]
            groups = [dict(row) for row in conn.execute("SELECT id,name FROM stock_groups ORDER BY position,id")]
            members = conn.execute("SELECT group_id,code FROM stock_group_members JOIN instruments "
                                   "ON instrument_id=instruments.id WHERE instrument_id NOT IN "
                                   "(SELECT instrument_id FROM stock_unfollowed) ORDER BY group_id,position,code,instruments.id").fetchall()
            recent = [row[0] for row in conn.execute("SELECT code FROM stock_recent_views JOIN instruments "
                      "ON instrument_id=instruments.id WHERE instrument_id NOT IN (SELECT instrument_id FROM stock_unfollowed) "
                      "ORDER BY viewed_at DESC,instrument_id DESC LIMIT 20")]
            selected = conn.execute("SELECT selected_group FROM stock_picker_preferences WHERE id=1").fetchone()[0]
        for stock in stocks:
            name = stock["name"] or ""
            stock["search"] = " ".join((stock["code"], name,
                "".join(lazy_pinyin(name)), "".join(lazy_pinyin(name, style=Style.FIRST_LETTER)))).lower()
        for group in groups:
            group["codes"] = [row["code"] for row in members if row["group_id"] == group["id"]]
        return {"stocks": stocks, "groups": groups, "recent": recent, "selected_group": selected}

    def is_unfollowed(self, code):
        with self.db.connection() as conn:
            return bool(conn.execute("SELECT 1 FROM stock_unfollowed u JOIN instruments i ON i.id=u.instrument_id "
                                     "WHERE i.code=?", (code,)).fetchone())

    def unfollowed(self):
        with self.db.connection() as conn:
            stocks = [dict(row) for row in conn.execute(
                "SELECT i.id,i.code,i.name,u.unfollowed_at FROM stock_unfollowed u "
                "JOIN instruments i ON i.id=u.instrument_id ORDER BY u.unfollowed_at DESC,i.code")]
            groups = conn.execute("SELECT u.instrument_id,g.id,g.name FROM stock_unfollowed_groups u "
                                  "JOIN stock_groups g ON g.id=u.group_id ORDER BY g.position,g.id").fetchall()
        for stock in stocks:
            identity = stock.pop('id')
            stock['groups'] = [{'id':g['id'], 'name':g['name']} for g in groups if g['instrument_id'] == identity]
        return stocks

    def change(self, command):
        if not isinstance(command, dict):
            raise ValueError("操作格式无效")
        action = command.get("action")
        if action not in {"create", "rename", "delete", "reorder", "reorder_members", "membership", "select", "visit", "unfollow", "restore"}:
            raise ValueError("未知分组操作")
        backup_before_update(self.db)
        with self.db.connection(write=True) as conn:
            def group_id(value):
                if type(value) is not int or not conn.execute("SELECT 1 FROM stock_groups WHERE id=?", (value,)).fetchone():
                    raise ValueError("分组不存在")
                return value

            def name(value):
                if not isinstance(value, str) or not 1 <= len(value.strip()) <= 20:
                    raise ValueError("分组名称须为 1 至 20 个字")
                value = value.strip()
                if value in {"最近查看", "全部股票", "未分组"}:
                    raise ValueError("此名称为固定分组保留")
                if conn.execute("SELECT 1 FROM stock_groups WHERE name=? AND id<>?",
                                (value, command.get("id", -1))).fetchone():
                    raise ValueError("分组名称已存在")
                return value

            if action == "create":
                conn.execute("INSERT INTO stock_groups(name,position) VALUES (?,(SELECT coalesce(max(position),-1)+1 FROM stock_groups))",
                             (name(command.get("name")),))
            elif action == "rename":
                identity = group_id(command.get("id"))
                conn.execute("UPDATE stock_groups SET name=? WHERE id=?", (name(command.get("name")), identity))
            elif action == "delete":
                identity = group_id(command.get("id"))
                conn.execute("DELETE FROM stock_groups WHERE id=?", (identity,))
                conn.execute("UPDATE stock_picker_preferences SET selected_group='all' WHERE selected_group=?", (f"group:{identity}",))
            elif action == "reorder":
                ids = command.get("ids")
                known = {row[0] for row in conn.execute("SELECT id FROM stock_groups")}
                if not isinstance(ids, list) or any(type(identity) is not int for identity in ids) or len(ids) != len(known) or set(ids) != known:
                    raise ValueError("排序必须包含所有自建分组且不能重复")
                conn.executemany("UPDATE stock_groups SET position=? WHERE id=?", enumerate(ids))
            elif action == "reorder_members":
                identity = group_id(command.get("id"))
                codes = command.get("codes")
                known = {row["code"]: row["instrument_id"] for row in conn.execute(
                    "SELECT code,instrument_id FROM stock_group_members JOIN instruments ON instrument_id=instruments.id "
                    "WHERE group_id=?", (identity,))}
                if not isinstance(codes, list) or any(not isinstance(code, str) for code in codes) or len(codes) != len(known) or set(codes) != set(known):
                    raise ValueError("排序必须包含该组全部股票且不能重复；归属发生变化时请重新选择分组")
                conn.executemany("UPDATE stock_group_members SET position=? WHERE group_id=? AND instrument_id=?",
                                 ((index, identity, known[code]) for index, code in enumerate(codes)))
            elif action == "membership":
                identity = group_id(command.get("id"))
                codes = command.get("codes")
                if not isinstance(codes, list) or not codes or len(codes) > 2000 or any(not isinstance(code, str) for code in codes):
                    raise ValueError("请选择要整理的股票")
                if type(command.get("add")) is not bool:
                    raise ValueError("分组归属操作无效")
                for code in dict.fromkeys(codes):
                    stock = conn.execute("SELECT id FROM instruments WHERE code=?", (code,)).fetchone()
                    if not stock:
                        raise ValueError(f"{code} 尚未查询，请先查询股票")
                    if command["add"]:
                        if conn.execute("SELECT 1 FROM stock_unfollowed WHERE instrument_id=?", (stock[0],)).fetchone():
                            raise ValueError(f"{code} 已取消关注，请在后台维护中恢复关注")
                        conn.execute("INSERT OR IGNORE INTO stock_group_members(group_id,instrument_id,position) "
                                     "SELECT ?,?,coalesce(max(position),-1)+1 FROM stock_group_members WHERE group_id=?",
                                     (identity, stock[0], identity))
                    else:
                        conn.execute("DELETE FROM stock_group_members WHERE group_id=? AND instrument_id=?", (identity, stock[0]))
            elif action in {"unfollow", "restore"}:
                codes = command.get('codes')
                if not isinstance(codes, list) or not codes or len(codes) > 2000 or any(not isinstance(code, str) for code in codes):
                    raise ValueError("请选择要整理的股票")
                identities = []
                for code in dict.fromkeys(codes):
                    stock = conn.execute("SELECT id FROM instruments WHERE code=?", (code,)).fetchone()
                    if not stock:
                        raise ValueError(f"{code} 尚未查询，请先查询股票")
                    identities.append(stock[0])
                if action == 'unfollow':
                    now = utc_now()
                    for identity in identities:
                        if conn.execute("SELECT 1 FROM stock_unfollowed WHERE instrument_id=?", (identity,)).fetchone():
                            continue  # Retried requests must preserve the original groups and timestamp.
                        conn.execute("INSERT INTO stock_unfollowed VALUES (?,?)", (identity, now))
                        conn.execute("INSERT INTO stock_unfollowed_groups SELECT instrument_id,group_id,position "
                                     "FROM stock_group_members WHERE instrument_id=?", (identity,))
                        conn.execute("DELETE FROM stock_group_members WHERE instrument_id=?", (identity,))
                        conn.execute("DELETE FROM stock_recent_views WHERE instrument_id=?", (identity,))
                else:
                    # Restore saved positions in order; retain the relative order of current members.
                    saved = conn.execute("SELECT instrument_id,group_id,position FROM stock_unfollowed_groups "
                                         "ORDER BY group_id,position,instrument_id").fetchall()
                    restoring = set(identities)
                    for member in saved:
                        if member['instrument_id'] not in restoring:
                            continue
                        conn.execute("UPDATE stock_group_members SET position=position+1 WHERE group_id=? AND position>=?",
                                     (member['group_id'], member['position']))
                        conn.execute("INSERT INTO stock_group_members(group_id,instrument_id,position) VALUES (?,?,?)",
                                     (member['group_id'], member['instrument_id'], member['position']))
                    conn.executemany("DELETE FROM stock_unfollowed WHERE instrument_id=?", ((identity,) for identity in identities))
            elif action == "select":
                selected = command.get("group")
                if selected not in {"recent", "all", "ungrouped"}:
                    if not isinstance(selected, str) or not selected.startswith("group:"):
                        raise ValueError("选择的分组无效")
                    group_id(int(selected[6:]))
                conn.execute("UPDATE stock_picker_preferences SET selected_group=? WHERE id=1", (selected,))
            elif action == "visit":
                stock = conn.execute("SELECT id FROM instruments WHERE code=?", (command.get("code"),)).fetchone()
                if stock and not conn.execute("SELECT 1 FROM stock_unfollowed WHERE instrument_id=?", (stock[0],)).fetchone():
                    conn.execute("INSERT INTO stock_recent_views VALUES (?,?) ON CONFLICT(instrument_id) "
                                 "DO UPDATE SET viewed_at=excluded.viewed_at", (stock[0], utc_now()))
        return self.read()
