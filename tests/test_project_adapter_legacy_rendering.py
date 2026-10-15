from yara_app.project_adapter import ProjectAdapter


def test_legacy_render_methods_exist_and_return_text(tmp_path):
    adapter = ProjectAdapter(tmp_path)
    adapter.ensure_runtime_files()

    status = adapter.render_status_text()
    messages = adapter.render_messages_text()
    diagnostics = adapter.render_diagnostics_text()
    page_text, chunk, total_pages = adapter.render_profiles_page(page=0, page_size=6)

    assert 'Worker' in status
    assert 'Telegram' in status
    assert status.count('\n') >= 5
    assert messages.strip()
    assert diagnostics.strip()
    assert page_text.strip()
    assert isinstance(chunk, list)
    assert total_pages >= 1
