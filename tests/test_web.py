import re
import unittest

from montrac.web import build_index_html


def active_script(html):
    return re.search(r"<script>([\s\S]*?)</script>", html, re.MULTILINE).group(1)


class WebTests(unittest.TestCase):
    def test_initial_state_is_embedded_and_script_has_no_placeholder(self):
        html = build_index_html(
            {
                "mode": "idle",
                "stations": [{"index": 3, "name": "Station 3", "port": "COM11"}],
                "segments": [],
                "events": [],
            }
        )

        self.assertIn("Station 3", html)
        self.assertIn("COM11", html)
        self.assertNotIn("__INITIAL_STATE_JSON__", html)
        self.assertRegex(html, r"window\.initialState = \{")

    def test_escape_html_quote_mapping_is_valid_javascript_source(self):
        script = active_script(build_index_html({}))

        self.assertNotIn('""":', script)
        self.assertIn('\'"\': "&quot;"', script)

    def test_disabled_buttons_do_not_use_progress_cursor(self):
        html = build_index_html({})

        self.assertNotIn("cursor: progress", html)
        self.assertIn("cursor: not-allowed", html)

    def test_config_inputs_have_id_and_name_attributes(self):
        script = active_script(build_index_html({}))

        self.assertIn('id="\' + nameId + \'" name="\' + nameId + \'"', script)
        self.assertIn('id="\' + portId + \'" name="\' + portId + \'"', script)

    def test_active_script_avoids_modern_javascript_syntax(self):
        script = active_script(build_index_html({}))

        self.assertNotRegex(script, r"\b(let|const)\b")
        self.assertNotIn("=>", script)
        self.assertNotIn("async function", script)
        self.assertNotIn("await ", script)
        self.assertNotIn("`", script)
        self.assertNotIn("??", script)
        self.assertNotIn("Object.fromEntries", script)
        self.assertNotIn(".closest(", script)


if __name__ == "__main__":
    unittest.main()
