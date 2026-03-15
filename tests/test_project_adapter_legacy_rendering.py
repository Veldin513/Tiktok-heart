from project_adapter import ProjectAdapter


def test_legacy_render_methods_exist_and_return_text(tmp_path):
    adapter = ProjectAdapter(tmp_path)
    adapter.ensure_runtime_files()

    status = adapter.render_status_text()
    messages = adapter.render_messages_text()
    diagnostics = adapter.render_diagnostics_text()
    page_text, chunk, total_pages = adapter.render_profiles_page(page=0, page_size=6)

    assert "Состояние проекта" in status
    assert "Пул сообщений" in messages
    assert "Диагностика" in diagnostics
    assert "Профили" in page_text
    assert isinstance(chunk, list)
    assert total_pages >= 1
