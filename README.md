# Agentforce Bulk & Regression Test Agent

Turns a user story into bulk test cases, runs them against a live
Agentforce (AgentScript) agent, grades pass/fail, flags hallucinated
responses, and catches regressions between runs -- via a Streamlit UI or
the CLI.

- **New here?** Start with `USER_GUIDE.md` (setup + how to use the app).
- **Want to know how it's built?** See `IMPLEMENTATION.md`.

Quick start:

```
pip install -r requirements.txt
copy .env.example .env      # fill in ANTHROPIC_API_KEY, org alias, agent id
launch_app.bat               # or: streamlit run app.py
```
