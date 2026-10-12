"""Offline acceptance for the actual demo helpers and Streamlit render paths."""

import ast
import base64
import datetime
import json
import os
from pathlib import Path
import re
import sys
import tempfile
from typing import Optional
import unittest
from types import SimpleNamespace

import pytz
import streamlit as st
from streamlit.testing.v1 import AppTest

FRONTEND = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(FRONTEND))
from stoc import stoc, normalize


def load_helpers():
    """load_helpers loads real frontend definitions without importing paid backend clients."""
    names = {
        "DemoFileIOHelper",
        "DemoUIHelper",
        "get_demo_dir",
        "DemoTextProcessingHelper",
        "_construct_citation_dict_from_search_result",
        "_display_main_article_text",
        "_display_references",
        "_display_persona_conversations",
        "_display_main_article",
        "clear_other_page_session_state",
    }
    tree = ast.parse((FRONTEND / "demo_util.py").read_text())
    nodes = [
        node
        for node in tree.body
        if isinstance(node, (ast.ClassDef, ast.FunctionDef)) and node.name in names
    ]
    namespace = dict(
        os=os,
        json=json,
        base64=base64,
        datetime=datetime,
        re=re,
        pytz=pytz,
        st=st,
        stoc=stoc,
        Optional=Optional,
        __file__=str(FRONTEND / "demo_util.py"),
    )
    exec(
        compile(
            ast.fix_missing_locations(ast.Module(body=nodes, type_ignores=[])),
            str(FRONTEND / "demo_util.py"),
            "exec",
        ),
        namespace,
    )
    return namespace


def app_source(body):
    """app_source creates a real Streamlit fixture using selected production helpers."""
    return (
        "import sys\nsys.path.insert(0, " + repr(str(Path(__file__).parent)) + ")\n"
        "import streamlit as st\nfrom test_frontend import load_helpers\n"
        "globals().update(load_helpers())\n" + body
    )


def load_article_page():
    """load_article_page returns the real MyArticles function with real offline UI helpers."""
    from streamlit_card import card

    path = FRONTEND / "pages_util" / "MyArticles.py"
    tree = ast.parse(path.read_text())
    nodes = [
        node
        for node in tree.body
        if isinstance(node, ast.FunctionDef) and node.name == "my_articles_page"
    ]
    namespace = dict(
        os=os, st=st, card=card, demo_util=SimpleNamespace(**load_helpers())
    )
    namespace.update(load_helpers())
    exec(
        compile(
            ast.fix_missing_locations(ast.Module(body=nodes, type_ignores=[])),
            str(path),
            "exec",
        ),
        namespace,
    )
    return namespace["my_articles_page"]


class FrontendCompatibilityTests(unittest.TestCase):
    """FrontendCompatibilityTests checks preserved article behavior with no LLM calls."""

    def test_polished_article_and_citations_are_assembled(self):
        """test_polished_article_and_citations_are_assembled verifies actual file-helper priority."""
        h = load_helpers()
        with tempfile.TemporaryDirectory() as folder:
            root = Path(folder)
            (root / "storm_gen_article.txt").write_text("# Raw\nraw")
            (root / "storm_gen_article_polished.txt").write_text(
                "# Polished\nhello [1]"
            )
            info = {
                "url_to_unified_index": {"https://example.com": 1},
                "url_to_info": {
                    "https://example.com": {
                        "title": "Reference",
                        "snippets": ["offline"],
                    }
                },
            }
            (root / "url_to_info.json").write_text(json.dumps(info))
            data = h["DemoFileIOHelper"].assemble_article_data(
                {f.name: str(f) for f in root.iterdir()}
            )
            self.assertIn("Polished", data["article"])
            self.assertEqual(data["citations"][1]["title"], "Reference")
            self.assertIsNone(h["DemoFileIOHelper"].assemble_article_data({}))

    def test_dialogue_and_citation_links_preserve_content(self):
        """test_dialogue_and_citation_links_preserve_content exercises actual text processing."""
        helper = load_helpers()["DemoTextProcessingHelper"]
        value = helper.parse_conversation_history(
            [
                {
                    "perspective": "Historian: Context",
                    "dlg_turns": [
                        {"user_utterance": "Question", "agent_utterance": "Answer [1]"}
                    ],
                }
            ]
        )
        self.assertEqual(value[0][:2], ("Historian", "Context"))
        self.assertEqual(value[0][2][1], {"role": "assistant", "content": "Answer"})
        self.assertEqual(
            helper.add_inline_citation_link(
                "Claim [1]", {1: {"url": "https://example.com"}}
            ),
            "Claim [[1]](https://example.com)",
        )
        self.assertEqual(normalize("Hello.World!"), "hello-world")

    def test_real_article_toc_and_currency_render(self):
        """test_real_article_toc_and_currency_render verifies Streamlit's actual output tree."""
        app = AppTest.from_string(
            app_source(
                '_display_main_article_text("# Title\\n## Details\\nCost $5 [1]", '
                '{1: {"url": "https://example.com"}}, None)\n'
            )
        ).run(timeout=15)
        self.assertEqual(len(app.exception), 0)
        values = [node.value for node in app.markdown]
        self.assertTrue(
            any("Cost \\$5 [[1]](https://example.com)" in v for v in values)
        )
        self.assertTrue(any('href="#details"' in v for v in values))

    def test_reference_selection_and_empty_panel(self):
        """test_reference_selection_and_empty_panel verifies interactive reference choice."""
        body = (
            '_display_references({1: {"title": "First", "url": "https://one.example", '
            '"snippets": ["one"]}, 2: {"title": "Second", "url": "https://two.example", '
            '"snippets": ["two"]}})\n'
        )
        app = AppTest.from_string(app_source(body)).run(timeout=15)
        self.assertEqual(len(app.exception), 0)
        app.selectbox[0].select("reference [2]").run(timeout=15)
        self.assertEqual(len(app.exception), 0)
        self.assertTrue(any("Second" in m.value for m in app.markdown))
        empty = AppTest.from_string(app_source("_display_references({})\n")).run(
            timeout=15
        )
        self.assertEqual(len(empty.exception), 0)
        self.assertIn("No references available", empty.markdown[0].value)

    def test_persona_conversation_renders_without_provider(self):
        """test_persona_conversation_renders_without_provider verifies chat presentation."""
        app = AppTest.from_string(
            app_source(
                '_display_persona_conversations([{"perspective": "Historian: Context", '
                '"dlg_turns": [{"user_utterance": "Question", '
                '"agent_utterance": "Answer [1]"}]}])\n'
            )
        ).run(timeout=15)
        self.assertEqual(len(app.exception), 0)
        self.assertEqual(app.tabs[0].label, "Historian")
        self.assertEqual(len(app.get("chat_message")), 2)
        self.assertTrue(any(m.value == "Answer" for m in app.markdown))

    def test_session_cleanup_preserves_active_page_and_unrelated_values(self):
        """test_session_cleanup_preserves_active_page_and_unrelated_values verifies rerun state."""
        app = AppTest.from_string(
            app_source(
                'st.session_state["page1_cache"] = "old"\n'
                'st.session_state["page2_cache"] = "active"\n'
                'st.session_state["selected_page"] = 2\n'
                "clear_other_page_session_state(2)\n"
                "st.json(dict(st.session_state))\n"
            )
        ).run(timeout=15)
        self.assertEqual(len(app.exception), 0)
        state = json.loads(app.json[0].value)
        self.assertNotIn("page1_cache", state)
        self.assertEqual(state["page2_cache"], "active")
        self.assertEqual(state["selected_page"], 2)

    def test_complete_article_panel_preserves_sidebar_and_conversation(self):
        """test_complete_article_panel_preserves_sidebar_and_conversation exercises the full article layout."""
        with tempfile.TemporaryDirectory() as folder:
            root = Path(folder)
            (root / "storm_gen_article_polished.txt").write_text(
                "# Polished\n## Evidence\nOffline claim [1]."
            )
            (root / "url_to_info.json").write_text(
                json.dumps(
                    {
                        "url_to_unified_index": {"https://example.com": 1},
                        "url_to_info": {
                            "https://example.com": {
                                "title": "Offline reference",
                                "snippets": ["source"],
                            }
                        },
                    }
                )
            )
            (root / "conversation_log.json").write_text(
                json.dumps(
                    [
                        {
                            "perspective": "Historian: Context",
                            "dlg_turns": [
                                {
                                    "user_utterance": "Question",
                                    "agent_utterance": "Answer",
                                }
                            ],
                        }
                    ]
                )
            )
            files = {f.name: str(f) for f in root.iterdir()}
            app = AppTest.from_string(
                app_source("_display_main_article(" + repr(files) + ")\n")
            ).run(timeout=15)
            self.assertEqual(len(app.exception), 0)
            self.assertTrue(any("Offline claim" in m.value for m in app.markdown))
            self.assertEqual(app.sidebar.selectbox[0].value, "reference [1]")
            self.assertTrue(
                any("Offline reference" in m.value for m in app.sidebar.markdown)
            )
            self.assertEqual(len(app.get("chat_message")), 2)

    def test_my_articles_card_preserves_title_image_and_pagination(self):
        """test_my_articles_card_preserves_title_image_and_pagination runs the real page and card component."""
        source = app_source(
            "from test_frontend import load_article_page\n"
            "st.session_state['page2_user_articles_file_path_dict'] = {'Offline_Article': {}}\n"
            "load_article_page()()\n"
        )
        app = AppTest.from_string(source).run(timeout=15)
        self.assertEqual(len(app.exception), 0)
        self.assertEqual(app.selectbox[0].value, 24)
        self.assertEqual(app.number_input[0].value, 1)
        cards = app.get("component_instance")
        self.assertEqual(len(cards), 1)
        args = json.loads(cards[0].proto.json_args)
        self.assertEqual(args["title"], "My Article")
        self.assertEqual(args["text"], "Offline Article")
        self.assertTrue(args["image"].startswith("data:image/"))
        self.assertEqual(args["styles"]["card"]["border-left"], "0.5rem solid #9AD8E1")


if __name__ == "__main__":
    unittest.main()
