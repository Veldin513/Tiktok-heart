from diagnostics_app import (
    classify_log_line,
    normalize_log_filter_mode,
    resolve_initial_tab_index,
    resolve_overview_layout,
)


def test_classify_log_line_detects_standard_levels():
    assert classify_log_line('15.03.2026 16:14:01 [INFO] Bot started') == 'info'
    assert classify_log_line('15.03.2026 16:14:01 [WARNING] Telegram disabled') == 'warning'
    assert classify_log_line('15.03.2026 16:14:01 [ERROR] HTTP 409 Conflict') == 'error'
    assert classify_log_line('15.03.2026 16:14:01 [DEBUG] DOM node cached') == 'debug'
    assert classify_log_line('15.03.2026 16:14:01 [INFO] ✅ Успех!') == 'success'


def test_resolve_initial_tab_index_defaults_to_overview():
    assert resolve_initial_tab_index(3, 6) == 0
    assert resolve_initial_tab_index('5', 6) == 0
    assert resolve_initial_tab_index(None, 6) == 0


def test_resolve_initial_tab_index_restores_only_when_explicitly_enabled():
    assert resolve_initial_tab_index(3, 6, restore_last_tab=True) == 3
    assert resolve_initial_tab_index(99, 6, restore_last_tab=True) == 5
    assert resolve_initial_tab_index(-10, 6, restore_last_tab=True) == 0


def test_normalize_log_filter_mode_maps_aliases_and_fallbacks():
    assert normalize_log_filter_mode('ERROR') == 'errors'
    assert normalize_log_filter_mode('warning') == 'warnings'
    assert normalize_log_filter_mode('ok') == 'success'
    assert normalize_log_filter_mode('critical') == 'important'
    assert normalize_log_filter_mode('unexpected') == 'all'


def test_resolve_overview_layout_collapses_secondary_panels_on_low_height():
    compact = resolve_overview_layout(1260, 430)
    assert compact['collapse_activity'] is True
    assert compact['collapse_recommendations'] is True
    assert compact['compact_metrics'] is True


def test_resolve_overview_layout_stacks_side_cards_on_narrow_width():
    layout = resolve_overview_layout(1100, 760)
    assert layout['stack_side_cards'] is True
    assert layout['collapse_activity'] is False
    assert layout['collapse_recommendations'] is False


def test_resolve_overview_layout_keeps_helper_cards_on_normal_window():
    normal = resolve_overview_layout(1260, 560)
    assert normal['collapse_activity'] is False
    assert normal['collapse_recommendations'] is False
