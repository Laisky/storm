Run python -m unittest discover -s frontend/demo_light/tests -v after installing
frontend/demo_light/requirements.txt. The regressions run real Streamlit AppTest
rendering for article text, TOC, reference selection, conversations and session
cleanup, the complete article sidebar layout and real card-component arguments.
File/text helpers use actual definitions loaded from demo_util.py.

The test loader excludes backend imports so importing a helper cannot start an
LLM or require provider credentials. This does not qualify full research
generation, browser-side component JavaScript or a published service.
