---
name: pre-push-scan
description: Scans the commits of a branch for private data before anything goes to the public repository. Use before a push or a pull request, or when asked whether a branch is safe to publish.
---

The repository is public. The branch's commits must hold no real names, company or customer
names, email addresses, local user paths, hostnames, credentials or recorded session data.

1. List what would be published (replace `master` with the branch's base):

   ```sh
   git log --format='%h %an <%ae>%n%B' master..HEAD
   git diff --stat master...HEAD
   ```

   The author line is published too. Check that it is the identity meant to be public.
2. Scan the added lines and the commit messages:

   ```sh
   git diff master...HEAD | grep -nE '^\+' | grep -nE '/Users/|/home/[a-z]|[A-Za-z0-9._%+-]+@[A-Za-z0-9.-]+\.[a-z]{2,}|BEGIN [A-Z ]*PRIVATE KEY|(api[_-]?key|secret|password|token)[^a-z]*[:=]'
   git log --format=%B master..HEAD | grep -nE '/Users/|/home/[a-z]|[A-Za-z0-9._%+-]+@[A-Za-z0-9.-]+\.[a-z]{2,}'
   ```

   Expected hits: the commit trailer `Co-Authored-By: Claude <noreply@anthropic.com>`, and the
   docs' placeholder `/home/dev/…`. Judge every other hit.
3. Read new or changed fixtures, recorded output and logs (`tests/fixtures/`, any `.json`,
   `.jsonl`, `.sql` or `.log` in the diff) in full: session data does not match a pattern.
4. Look for names that a pattern cannot know: people, companies, customers, machines and
   projects that are not placeholders (`my-app`, `fix-login-3f2a`).
5. Report every finding with its file and line, or its commit.
