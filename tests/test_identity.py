from __future__ import annotations

import os
import tempfile
import unittest
from unittest.mock import patch

import numpy as np

from fireball import settings, storage, voices
from fireball.daemon.core import DaemonCore


class MyVoiceTests(unittest.TestCase):
    """Qual das vozes cadastradas é a de quem usa o Fireball.

    É o que junta num participante só a track do microfone ("Você"), o nome
    que a diarização reconheceu e o convidado da agenda — sem isso a mesma
    pessoa aparece três vezes na ficha da reunião.
    """

    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.env = patch.dict(os.environ, {"FIREBALL_HOME": self.temp.name})
        self.env.start()
        self.core = DaemonCore()

    def tearDown(self):
        self.env.stop()
        self.temp.cleanup()

    def _voice(self, name, emails):
        profile = voices.save_profile(
            name,
            np.array([1.0, 0.0, 0.0], dtype=np.float32),
            model="test-model",
            email=emails[0] if emails else "",
        )
        if len(emails) > 1:
            profile = voices.rename_profile(profile["id"], name, emails)
        return profile

    def test_defaults_to_nobody(self):
        self.assertEqual(settings.load()["my_voice"], "")
        profile = self.core.profile()
        self.assertEqual(profile["voice_id"], "")
        self.assertEqual(profile["name"], "")

    def test_profile_takes_the_name_and_emails_of_the_chosen_voice(self):
        voice = self._voice("Rafael Telles", ["Rafael.Telles@Layers.Education"])
        settings.save(my_voice=voice["id"])

        profile = self.core.profile()
        self.assertEqual(profile["name"], "Rafael Telles")
        self.assertEqual(profile["voice_id"], voice["id"])
        # normalizado em minúsculas: é assim que a junção por e-mail compara
        self.assertIn("rafael.telles@layers.education", profile["emails"])

    def test_the_calendar_account_joins_the_emails(self):
        voice = self._voice("Rafael Telles", ["rafael@pessoal.com"])
        settings.save(my_voice=voice["id"], gog_account="rafael.telles@layers.education")

        emails = self.core.profile()["emails"]
        self.assertIn("rafael@pessoal.com", emails)
        self.assertIn("rafael.telles@layers.education", emails)

    def test_the_self_attendee_names_me_when_no_voice_is_chosen(self):
        """Sem voz escolhida, o convidado marcado como `self` ainda diz quem eu
        sou — é o que faz a junção funcionar em reunião vinda da agenda."""
        settings.save(calendar_provider="gog")
        storage.write_json(
            storage.fireball_home() / "agenda.json",
            {
                "provider": "gog",
                "status": "ok",
                "fetched_at": "2026-08-30T13:00:00",
                "fetched_at_epoch": 1,
                "error": None,
                "events": [
                    {
                        "id": "e1",
                        "attendees": [
                            {"name": "Rafael Telles", "email": "rafael@layers.education", "self": True},
                            {"name": "Marina Alves", "email": "marina@vertico.com.br"},
                        ],
                    }
                ],
            },
        )
        profile = self.core.profile()
        self.assertEqual(profile["name"], "Rafael Telles")
        self.assertEqual(profile["email"], "rafael@layers.education")

    def test_the_chosen_voice_wins_over_the_calendar(self):
        voice = self._voice("Rafael T.", ["rafael@layers.education"])
        settings.save(my_voice=voice["id"], calendar_provider="gog")
        storage.write_json(
            storage.fireball_home() / "agenda.json",
            {
                "provider": "gog",
                "status": "ok",
                "fetched_at": "2026-08-30T13:00:00",
                "fetched_at_epoch": 1,
                "error": None,
                "events": [
                    {"id": "e1", "attendees": [{"name": "Rafael Telles Layers", "self": True}]}
                ],
            },
        )
        # escolha explícita de quem usa vale mais que o displayName do convite
        self.assertEqual(self.core.profile()["name"], "Rafael T.")

    def test_deleting_the_chosen_voice_releases_the_setting(self):
        """Configuração apontando para um perfil que não existe mais é estado
        quebrado, e arrumá-lo é trabalho de quem apagou."""
        voice = self._voice("Rafael Telles", ["rafael@layers.education"])
        settings.save(my_voice=voice["id"])

        self.core.delete_voice(voice["id"])
        self.assertEqual(settings.load()["my_voice"], "")
        self.assertEqual(self.core.profile()["voice_id"], "")

    def test_deleting_another_voice_leaves_the_setting_alone(self):
        mine = self._voice("Rafael Telles", ["rafael@layers.education"])
        other = self._voice("Marina Alves", ["marina@vertico.com.br"])
        settings.save(my_voice=mine["id"])

        self.core.delete_voice(other["id"])
        self.assertEqual(settings.load()["my_voice"], mine["id"])

    def test_a_dangling_id_does_not_break_loading(self):
        """Perfil apagado fora do app (na mão, em outra máquina) deixa o id
        pendurado: ele tem de virar "não configurado", não erro."""
        settings.save(my_voice="perfil-que-nao-existe")
        self.assertEqual(settings.load()["my_voice"], "perfil-que-nao-existe")
        profile = self.core.profile()
        self.assertEqual(profile["name"], "")
        self.assertEqual(profile["emails"], [])


if __name__ == "__main__":
    unittest.main()
