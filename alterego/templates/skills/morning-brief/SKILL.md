---
name: morning-brief
description: Prepare a short morning brief from memory, daily logs and the todo list — what's on today, what's overdue, what needs the user's attention.
---

# Skill: Morning Brief

A playbook for starting the user's day with signal, not noise.

## When to use

- The user asks "what's on today", "morning brief", or similar.
- A scheduled daemon check asks for a morning brief.

## Steps

1. Call `get_time` so dates and "today" are grounded, never guessed.
2. Call `todo_list` and read the current list.
3. Use `memory_search` for "today", plus any project names from the todo
   list, to pull relevant context from MEMORY.md and recent daily logs.
4. Present, in this order, briefly:
   - **Today** — commitments and deadlines landing today.
   - **Overdue** — anything past due, stated plainly without nagging.
   - **Worth attention** — at most one item: a risk, an opening, or a
     decision that is quietly waiting on the user.
5. If there is genuinely nothing, say so in one line. Never pad a brief.

## Rules

- Facts come from memory files and tools only — never invent meetings,
  deadlines or people.
- Keep it under 10 lines unless the user asks for detail.
- End with an offer to act on one item ("Want me to draft that reply?"),
  not a list of questions.
