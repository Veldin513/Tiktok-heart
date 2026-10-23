from scripts import import_chrome_session as cli


def test_cli_uses_the_validated_importer(monkeypatch, capsys):
    calls = []

    class Adapter:
        def __init__(self, _root):
            pass

        def import_google_chrome_profile(self, **kwargs):
            calls.append(kwargs)
            return {'destination': 'new-profile', 'backup': 'old-profile'}

    monkeypatch.setattr(cli, 'ProjectAdapter', Adapter)
    assert cli.main(['--chrome-profile', 'Profile 1', '--bot-profile', 'main']) == 0
    assert calls == [{'chrome_profile_id': 'Profile 1', 'bot_profile_key': 'main', 'copy_mode': 'full_profile'}]
    assert 'old-profile' in capsys.readouterr().out


def test_cli_reports_rejected_import_as_failure(monkeypatch, capsys):
    class Adapter:
        def __init__(self, _root):
            pass

        def import_google_chrome_profile(self, **_kwargs):
            raise RuntimeError('protected session')

    monkeypatch.setattr(cli, 'ProjectAdapter', Adapter)
    assert cli.main([]) == 1
    assert 'protected session' in capsys.readouterr().err
