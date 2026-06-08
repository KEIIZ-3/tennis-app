def apply_court_type_policy():
    """
    コート種別の表示名・選択肢を運用向けに調整します。

    DB上の保存値:
    - sono: 西猪名公園
    - amagasaki: 尼崎記念公園
    - other: その他

    既存データの "sono" はそのまま使い、管理サイト上の表示だけ
    「西猪名公園テニスコート」から「西猪名公園」に変更します。
    """
    try:
        from .models import Court
    except Exception:
        return

    court_type_choices = (
        ("sono", "西猪名公園"),
        ("amagasaki", "尼崎記念公園"),
        ("other", "その他"),
    )

    Court.COURT_SONO = "sono"
    Court.COURT_AMAGASAKI = "amagasaki"
    Court.COURT_OTHER = "other"
    Court.COURT_TYPE_CHOICES = court_type_choices

    try:
        field = Court._meta.get_field("court_type")
        field.choices = court_type_choices
        field.default = "sono"
    except Exception:
        pass
