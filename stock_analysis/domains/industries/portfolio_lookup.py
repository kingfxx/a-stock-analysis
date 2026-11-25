"""Read the latest published Shenwan membership valid on a selected date."""


def read_classification(db, code, as_of):
    with db.connection() as conn:
        row = conn.execute("""
            WITH current_import AS (SELECT member_import_id FROM sw_imports ORDER BY id DESC LIMIT 1),
            effective_membership AS (
                SELECT industry_code, effective_date FROM sw_membership_history
                WHERE import_id=(SELECT member_import_id FROM current_import)
                AND stock_code=? AND effective_date<=?
                UNION
                SELECT industry_code,effective_date FROM sw_memberships
                WHERE import_id=(SELECT member_import_id FROM current_import)
                AND stock_code=? AND effective_date<=?
            )
            SELECT node.name, coalesce(top.name, parent.name, node.name) AS sector, m.effective_date
            FROM effective_membership m JOIN sw_industries node ON node.code=m.industry_code
            LEFT JOIN sw_industries parent ON parent.code=node.parent_code
            LEFT JOIN sw_industries top ON top.code=parent.parent_code
            ORDER BY m.effective_date DESC, node.code LIMIT 1
        """, (code, as_of, code, as_of)).fetchone()
    return dict(row) if row else None
