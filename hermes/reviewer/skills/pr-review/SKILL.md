---
name: pr-review
description: "Review one OpenMRS or O3 pull request with evidence, then post one GitHub review with inline comments through post_pr_review."
---

# PR Review

One webhook run reviews one pull request at one commit: the repository, number, and head commit in the review target line of the first message. Nobody answers questions during the run. The result is one public COMMENT review, posted with `post_pr_review`: the review body (see Review Body) plus one inline comment per finding (see Comment Voice). Your final text answer goes to the operators' log only.

Every finding passes the Finding Validation Gate before it is drafted, and high-stakes findings also pass Adversarial Verification. The checks for rarer kinds of change are in Reference Checks below; run a section when its trigger fires.

## Workflow

1. Set up the target.
    - Call `prepare_workspace`. If it fails, stop and answer with its error.
    - Read the pull request: `gh pr view <n> -R <repo> --json title,body,author,files,commits,headRefOid,baseRefName,labels,comments,reviews` and `gh pr diff <n> -R <repo>`. Read the inline review threads with `gh api repos/<repo>/pulls/<n>/comments`.
    - Clone and check out the commit from the review target line, not the branch tip: `git clone --filter=blob:none https://github.com/<repo> ~/repos/<name>`, `git -C ~/repos/<name> fetch origin pull/<n>/head`, `git -C ~/repos/<name> worktree add ~/repos/<name>-pr<n> <sha>`.
    - Decide if the change is safe to build (see the SOUL). If it is not, do steps 2 to 4 and 6 to 8 from the diff and the code only, skip every install, build, test, and app run, and say so in the body's "Not covered" line.
    - Done when the head commit is checked out and the safe-to-build decision is made.

2. Establish the scope.
    - Read the title, body, changed files, commits, and the discussion surface (see Discussion Review Gate).
    - When the PR names a Jira ticket (O3-1234, TRUNK-1234), read it with `curl -s 'https://openmrs.atlassian.net/rest/api/2/issue/<KEY>?fields=summary,description,status,comment,issuelinks'` (no login needed; `issues.openmrs.org` redirects to an HTML page, so do not use it). Find its pull requests with `gh search prs --owner openmrs <KEY>`; Jira's own list of linked pull requests is not visible without a login. Read a status as a pointer: "PR Pending" or "In Progress" names work that exists somewhere. Compare its dates with this PR; a status set months before this PR was opened almost always refers to a different PR. If the ticket cannot be read, say so in "What I checked".
    - Classify the review mode: frontend UI/workflow, frontend API/config contract, backend REST/API contract, backend implementation, distro/release coordination, docs/translations, or CI-only. The mode chooses the evidence and the Maintainer Question Inventory groups.
    - Frontend API/config changes need backend contract checks. Backend REST/API changes need representation, validation, authorization, and consumer checks. Release changes need dependency and version-order checks.
    - Done when the mode is classified, the discussion surface is read, and you know what evidence each changed area needs.

3. Inspect the code, in two passes.

    **Domain pass:**
    - Review the changed code plus nearby callers, tests, fixtures, config, and translations.
    - For frontend PRs that touch API calls or backend behavior, clone the owning `openmrs-module-*` repo and decide every backend claim per Backend Checks, not from the frontend TS types.
    - When a PR swaps a base class, interface, service shape, resource parent, API wrapper, or schema owner, run Contract Preservation Checks.
    - When a PR adds public API (methods, constants, types) to a platform module or shared library, run API Surface Review.
    - When the PR adds a mechanism (a config key, a permission check, a hook, a helper, a wrapper component, a flag), run the Existing Mechanism Check before you review how the mechanism is built.
    - When changed code reads a config key, run Config Contract Checks, also for keys the PR did not touch.
    - When the PR starts to rely on backend behavior from a specific module version, run the Backend Version Floor Check on `routes.json`, also when the PR does not touch that file. A commit message that says "requires module X.Y.Z" is the loudest trigger.
    - For every new file, run the Repo Convention Checks against two or three sibling files.
    - For frontend UI/workflow and backend PRs, run the Maintainer Question Inventory groups the mode selects, as their own step.

    **Breadth pass:** read the diff again, once for each lens, and write down candidate findings:
    - general correctness and the repo's own conventions;
    - silent failures: swallowed errors, falsy returns that hide a failure, fallbacks that hide a misconfiguration;
    - tests: do they prove what they claim, and does a risky change have a test that fails without it;
    - type design: do new types state their invariants, or accept states that cannot be valid;
    - comments and docs: does each comment, name, and description still match what the code does.
    - On a short, simple diff, one combined read is enough. Complexity, not size, decides: a short diff that changes concurrency, data integrity, a backend contract, or shared framework API gets every lens.
    - Done when every changed file has had both passes.

4. Merge and gate.
    - Pool the findings from both passes, remove duplicates, and drop style-only noise.
    - Run each one through the Finding Validation Gate, and through Contract Preservation Checks when that trigger fired.
    - Apply the Discussion Review Gate: drop anything already raised on the PR, or reframe it.
    - Done when each candidate is kept, reframed, or dropped with a reason. Keep the dropped ones with their reasons for the final answer.

5. Validate before judging (skip what the safe-to-build decision rules out).
    - **Reproduce every finding before you draft it,** with a focused test, a live request, or the running app. A finding you could not reproduce gets `not reproduced` in the "What is wrong" cell, and its comment is a question.
    - **Find the smallest reproduction.** Take away each step (an extra open form, extra data, a second page) and run it again. Keep only the steps without which the defect does not show.
    - **Close the open questions.** If one more run settles a point ("does it also happen on the base commit", "does the fix keep the user's data"), run it. Keep "not tested" only for what cannot be run here, and say why.
    - **Behavior the PR does not cause,** which the base commit shows the same way, is dropped (see the Finding Validation Gate).
    - **Run every suggestion block before you post it,** including test code. Apply it in the worktree, run the affected tests, lint, and type check, then restore the file with git. A suggestion you could not run is not posted as a suggestion block.
    - Run the repo's lint, type check, and the affected tests on the PR commit, and give each one a row in "What I checked".
    - In OpenMRS frontend repos, run every tool through yarn, never npx or npm. Read the package's `package.json` scripts first to learn the test runner (patient chart uses vitest, not jest). Install with `yarn install --immutable` at the repo root, then run from the package folder: `yarn vitest run <path>`, `yarn tsc`, `yarn eslint <path>`.
    - A fix that lives in another repo (esm-core) can be tried in the built file the dev server serves: find the line in `node_modules/@openmrs/esm-app-shell/dist/*.js`, break the yarn cache hard link with `cp f f.tmp && mv f.tmp f`, change the line, open a new browser session, and run the scenario.
    - For a PR that fixes a bug, run the Fix-Location Gate before you judge where the fix lives.
    - **openmrs-core changes:** build with the JDK the branch's pom.xml and CI use, and run the affected modules' tests (`mvn -B -pl <module> -am test -Dtest=<Class>`; add `-Dsurefire.failIfNoSpecifiedTests=false` when `-am` pulls in modules without that class). Context-sensitive tests start Spring and Liquibase on an in-memory database, so they catch some wiring and changeset failures. The workspace has no database server and no OpenMRS SDK, so a full server start is not possible: say so in "Not covered" for any change to Spring wiring, module startup, or Liquibase changesets.
    - **REST changes:** the PR's own code does not run on dev3. Use dev3 only to learn how the current release behaves (the "before"), and prove the "after" with the PR's tests or a test you write.
    - For release-dependent changes, check the paired backend, distro, or module PR or version that makes the change safe. Keep merge order separate from code correctness (see Release Coordination Notes).
    - **On any PR that changes what a user sees, run it.** A change to the data that a screen lists (a new filter, a changed request) counts, and a live API check does not replace a run of the app. Start `yarn openmrs develop --backend https://dev3.openmrs.org` in the changed package as a background process, wait until it prints its URL, log in through the login page (dev3's demo account, `admin` / `Admin123`), and drive the changed screen with agent-browser. Open menus, expand rows, submit forms: crashes that fire only on an interaction, and layout breaks in a borrowed component, are invisible to `tsc`, tests, and reading.
    - For layout and CSS findings, compare against dev3's own app (https://dev3.openmrs.org/openmrs/spa), which is the shipping build, not only the local dev server; the Carbon version can differ. Measure the rendered geometry rather than judge a screenshot.
    - Done when every surviving finding has concrete evidence (backend code, a test, a static code path, or UI behavior you saw), or is marked `not reproduced`.

6. Adversarially verify high-stakes findings (see Adversarial Verification). Done when each one survived refutation or was dropped or reframed, with the reason kept for the final answer.

7. Draft the review.
    - Severity decides what is worth posting: correctness, data integrity, workflow regressions, security, and missing tests earn a place; style-only points do not unless they materially help maintainability or contributor clarity.
    - Do not fish for comments. If the PR is correct after validation, the review has no inline comments, and the body says so with the evidence.
    - Scale the nit bar to the diff. On a small, single-purpose fix that validates clean, optional restructures and comment-placement points are not worth a thread; drop them.
    - **Report the cause, not each symptom.** When two or more findings share one cause (the same config key, helper, or assumption), post the cause, on the line that needs the fix.
    - Write one inline comment per finding per Comment Voice, then the body per Review Body.
    - **Name the single point in one sentence before you write anything else.** That sentence plus the suggestion block is the comment.
    - **Anchor each comment where the fix goes,** on the commit from the review target line. Prefer a line inside a diff hunk. For a point about a whole file (orphaned config keys, a dead export), anchor on the first changed line of the file. When the fix line is outside the diff, anchor on the nearest changed line in the same file and name the target line in the comment ("line 88 below"). Never drop a finding because its line is outside the diff.
    - **Design the anchor and the suggestion block together.** A suggestion block replaces exactly the lines from `start_line` to `line` (or `line` alone). Make the block valid for that range. Prefer the smallest insertion that carries the fix over replacing correct lines around it. A fix outside the anchored range goes in a plain fenced block, not a suggestion block.
    - **Proofread each block as code.** Copy the anchored lines verbatim and change only what the fix changes. Check for a dropped line-continuation token (a trailing `+`, `&&`, comma, or backslash) and for smart quotes. A committed suggestion goes straight into the branch.
    - **When the edits are not next to each other, do not force a suggestion block.** List the items in a plain fenced code block and name the files.
    - **One ask per comment.** A second improvement is a separate comment, or it is dropped.
    - Inline comments never carry process notes ("could not run tsc", "did not check the shipping build"). Those go in the body's "What I checked" and "Not covered" only.
    - Done when every kept finding has one comment and the body is written.

8. Check for staleness, then post.
    - Compare the head now (`gh pr view <n> -R <repo> --json headRefOid`) with the commit you reviewed. If it moved, diff `<reviewed>..<new head>` and drop every finding whose code the new commits changed or fixed. Keep the comments anchored to the reviewed commit; `post_pr_review` posts for that commit.
    - Call `post_pr_review` once, with the body and every inline comment: `[{path, line, side?, start_line?, body}]`. `side` is `RIGHT` (the default, the new version) or `LEFT` (a removed line).
    - If GitHub refuses the review with a 422 about a line ("Line could not be resolved"), one comment's line is not in the diff. Move that comment to the nearest changed line in the same file, name the target line in its text, and call `post_pr_review` again. After any other failure, do not retry.
    - Done when `post_pr_review` returns the review URL, or failed in a way that must not be retried.

9. Answer. Your final text goes to the operators' log. Give the review URL, each dropped finding with its reason in one line, and each record you created on dev3 (patient, visit, order, encounter) with its uuid.

## Discussion Review Gate

The **discussion surface** is everything already on the PR: inline threads, prior reviews, maintainer replies, top-level comments, and other bots' reviews. Read it before you draft anything.

- Do not raise again points the contributor already addressed.
- Do not duplicate open feedback unless the existing comment is wrong or incomplete in a way that changes the requested fix.
- Do not reopen decisions a maintainer already settled unless new evidence changes the risk.
- Factual claims in existing comments, from people or tools, are unverified findings, not evidence. Before you build on one, check its claims (endpoints, response shapes, versions, runtime behavior) against the authoritative source. A prior comment refuted by evidence gets a kind correction, with the evidence, as a comment in your review.

## Finding Validation Gate

Run this before you draft any comment, on findings from both passes:

- Did this PR introduce it? If not, drop it before drafting, and do not offer it as an inline comment. List it in one line under "Not covered" in the review body ("Not from this PR: ...").
- Has a newer PR already fixed it? Search open and recently merged PRs for the same file and defect (`gh pr list -R <repo> --state all --search "<file or keyword>"`). If one exists, drop the finding.
- Does it matter on the current release and on dev3? Drop findings that only affect old releases.
- Is there a real impact on users, data integrity, workflow correctness, CI, security, maintainability, or contributor clarity?
- What evidence proves it: backend code, a local test, a static code path, CI or logs, or UI behavior?
- Have you traced why the code exists (git blame, the introducing commit, linked discussion) before you critique its design? A reactive patch (a lint or bot suggestion bundled into an unrelated commit) is not a design decision.
- Does the code do what it says? A prop, name, comment, or type that contradicts the behavior is a defect, not a style point.
- Existing code with the same flaw explains how the flaw got here; it never justifies keeping it. A PR that copies a flawed pattern into a new place introduces that defect. Precedent may set the scope of the ask, never whether it is wrong.
- Anything the PR's changes cause, expose, or sit next to on the same lines is an ask in this PR, not a follow-up.
- Does a test-gap finding describe a scenario a real user or client can produce? A test for a state that cannot happen is not a finding.
- What is the smallest safe fix the contributor can make?
- Does the fix follow a pattern the repo already uses? Search the repo for how sibling code handles the same situation (a date normalized with `dayjs().startOf('day').utc()`, a header comment on a translations file that must not be deleted, a snackbar for a failed action) and write the fix in that shape.

If a finding fails the gate, drop it or reframe it as a question.

## Adversarial Verification

The gate above is a self-check. This is a stronger pass for findings that would block the PR, or that touch a backend or REST contract, data integrity, workflow correctness, or security. Skip it for style, docs, and low-risk single-file findings.

For each such finding, switch roles: your only job is now to prove the finding wrong, and you call it refuted when the evidence is not conclusive.

- Use authoritative evidence. For a backend or contract claim, read the full handler in the owning module per Backend Checks, and for runtime behavior, call the live endpoint. Code reading alone is not conclusive when registration, handler order, or AOP wiring can change the outcome. For a frontend claim, read the full surrounding code, callers, and tests.
- Hunt the counter-evidence a confirming re-read skips: a field still exposed through `@PropertyGetter`, a representation the frontend does not request, behavior already covered by a test or an open thread, an inherited base-class default that still applies, backend code that never fires at runtime (an unregistered bean, a `@Handler` order tie, a guard that depends on another handler).
- For the riskiest findings, ask three separate questions and drop the finding if two say no: Does it reproduce? Is it already handled elsewhere? Does the behavior change for a client?

A finding that survives is drafted. A refuted finding is dropped or reframed as a question, never posted as a blocker; keep the reason for the final answer.

## Existing Mechanism Check

Run this when the PR adds a mechanism for a capability: gating, configuration, caching, data fetching, formatting, validation, retries, permissions.

- **Name the capability, not the code.** One sentence: what the new code gives the user or implementer. Search for that capability, not the PR's identifiers.
- **Search the layers below the PR,** in order: the framework or shared library (openmrs-esm-core), the backend module, then sibling apps in the same repo. A capability that an implementer can turn on through configuration counts as provided.
- **Read the source doc comment of every framework API the PR calls.** It often says when not to use the API, or names the one to use instead.
- **Find every other reader of the thing the mechanism protects or changes,** including in other repos, and check whether it goes around the mechanism.
- **Compare the coverage** of the existing and the new mechanism. Do not assume the platform version is better.

If the platform already provides it with the same or wider coverage, ask about it with the configuration or call that does the same job. If the new mechanism leaves a reader uncovered, name the reader, the path a user takes to reach it, and the smallest fix in this PR. If neither, there is no finding; record the search in "What I checked".

## Repo Convention Checks

`tsc`, `eslint`, and the tests pass on code that breaks house style. Before you call a new file clean, open two or three sibling files in the same package and compare:

- Import grouping and order: React, then external packages, then `@openmrs/*`, then local; within local, components before hooks before stores before types before styles.
- Names of files, test files, and exported symbols, against the neighbours. O3 file suffixes encode the kind: `.modal.tsx`, `.workspace.tsx`, `.resource.ts`, `.ts` for files without JSX.
- Whether shared behavior already has a helper that the new code writes again.
- SCSS: `@use` order, design tokens over literals, and whether a removed rule left an import unused.
- A test file tests the module it is named after. A test for another module in it (a hook test in a component test file) is a finding. A `vi.importActual` in a test body, used to get past the file's own `vi.mock`, is the usual sign.

## Reference Checks

### Fix-Location Gate

Run it for any PR that fixes a bug, especially state that should change when a visit, encounter, order, or queue entry changes and is fixed on the frontend. Run it before you judge details of the fix.

- If the unchanged code around the fix already sends the right refresh signal (cache invalidation, a `queue-entry-updated`-style event) and the bug still reproduces, the server state is wrong. Go to the owning backend module first.
- Reproduce the bug through the backend alone, with REST calls against dev3. If the wrong state appears there, it is a backend bug, and a frontend fix is a workaround for one client.
- When the cause is in another repo, search that repo's open PRs and branches for the ticket and the defect (`gh pr list -R <repo> --search <ticket>`). A fix may already exist.
- Read a live probe only after every setup step in it succeeded. A failed create, or leftover state from an earlier probe, looks like a negative result.
- When a lifecycle behavior half-works, probe the working sibling path (visit stop closes queue entries, visit void does not). It usually points at the exact broken condition.
- Do not recommend moving a fix to the backend without a concrete mechanism: the class, the failing guard or order, and the smallest backend change.
- When the fault lives in another repo, the ask in this PR is a guard the author can land here: an e2e test that fails when the other repo regresses, a defensive check, a fixture that fixes the contract. Hedge a cross-repo cause in the comment ("I suspect it is because..."), even when you are sure.

### Contract Preservation Checks

When a PR changes a REST resource, service interface, base class, API client, config schema, or DTO:

- What did the previous inherited or shared abstraction give that the new code must keep?
- Did representation fields, creatable and updatable properties, paging, search, retired and `includeAll` behavior, validation, authorization, error shapes, side effects, and subclass hooks stay compatible?
- Are frontend consumers, backend callers, and test fixtures still aligned?
- If behavior is reimplemented by hand, is there a focused test that would catch a missing field?

Before you flag a missing test, confirm the logic is not tested elsewhere. Thin wrapper or re-export files (a `*.workspace` file that only mounts a component tested in its own `*.test.tsx`) do not need their own tests.

### API Surface Review

When a PR adds public API to a platform module or shared library (semver freezes what ships), ask for each new member: what do its callers consume, and what is the smallest, most domain-shaped surface that serves them?

- Answer from the callers in the diff, not the docs. If no caller uses what the member exposes (they discard it, only null-check it, or convert it), it exposes a mechanism instead of the result.
- Docs that must caveat the behavior (which of several results comes back, ordering disclaimers) usually mark a leaking abstraction.
- Public constants, types, or overloads that exist only to feed the new member belong behind it.
- A name that describes the mechanism rather than the caller's intent is a second signal.
- Before you propose another shape, confirm the call sites have the data it needs.

### Config Contract Checks

For each config key the changed code reads, including keys the PR did not touch:

- Does the key's description promise the meaning the call site depends on? The `_default` is not the contract; the description is. A key described as "the default status for the queues eg Waiting" does not promise the waiting status. A component that shows a list captioned "Waiting" from it is the finding, and the fix is a new key whose description states that meaning.
- Grep every reader and writer of the key, and check that the description covers each use.
- Does the name describe the domain or the mechanism, and does it match its siblings (all end in `ConceptUuid`)?
- `@openmrs/esm-config` has no alias or migration for a renamed key, so a rename silently reverts every distro that set it. A rename is a follow-up with a release note, never a change inside a feature PR.

When the key is older than the PR and the PR does not change how it is read, drop the finding.

### Backend Version Floor Check

When changed frontend code starts to need backend behavior from a specific module version (a new endpoint or search parameter, a changed response format, a fix the code now assumes):

- Check `backendDependencies` and `optionalBackendDependencies` in the app's `routes.json`. It goes stale without appearing in the diff.
- The floor must cover the version that added the behavior.
- Write the floor as `>=X.Y.Z-0`, not `>=X.Y.Z`. The framework checks with `semver.satisfies(installed, range, { includePrerelease: true })`, and `X.Y.Z-SNAPSHOT` sorts below `X.Y.Z`, so a plain floor rejects every SNAPSHOT server (dev3 included). Prove a range with `node -e "console.log(require('semver').satisfies('X.Y.Z-SNAPSHOT', '<range>', {includePrerelease: true}))"` from a repo that has semver installed.
- Keep the existing upper bound and range style; move the floor only.

## Maintainer Question Inventory

Questions that senior OpenMRS maintainers ask, which the evidence-driven gates do not surface. Route by mode: backend PRs get Design, Correctness, Tests, and Ecosystem; frontend UI/workflow PRs get Correctness, Frontend quality, Performance, and Tests; frontend API/config PRs add Design and Ecosystem; distro PRs get Ecosystem; purely visual changes get Frontend quality. Run Correctness and Tests first. For docs, translation, and CI-only PRs, skip this and say so in "What I checked".

**Design and API surface**
- Do new signatures use typed domain objects where they could, rather than bare uuids and strings?
- Does each name state exactly what the member takes and returns? A misleading name is a defect.
- How does the nearest existing code do the same thing? A second way needs a reason.
- For an external standard (FHIR, HL7, UCUM), check field semantics and bindings against the spec.
- Config that duplicates a backend property should use the property.
- A new endpoint, DTO, or wrapper needs proof that the existing resource cannot express it through parameters, representations, or status changes.
- Hold layering rules even when the violation works (a domain object calling a service, a data hook rendering UI errors). A diff that fans out over many callers usually belongs in the shared abstraction.
- Can an implementer adjust the behavior without a fork? Config schemas need defaults and descriptions a non-developer can read.

**Correctness and data integrity**
- Does the feature work in every lifecycle mode: enter, edit, view, void or retire, re-entry, and when the data is multiple where the code assumes one?
- Does the code read the data model's explicit flag, or infer state from display strings? Are two related states mixed up (a patient in a bed is not thereby admitted)?
- Name the dangerous state the change could produce (data shown for the wrong patient, two active visits) and confirm it cannot be reached.
- Authorization on new endpoints and service methods; redirect and injection surface on URL- or input-derived values; patient data in logs, URLs, exports, and printouts.
- Never hand consumers a partly populated persistent object; prove a re-save cannot destroy data a query filtered out.
- Persisted references use uuids, not names.
- Before retiring, deleting, or changing shared metadata, check what else references it.
- Reason about nulls from what the database permits, not what the UI usually sends.
- New behavior is configurable and defaults to what the system does today.
- Is an exception swallowed into a log or a falsy return? A falsy result and a failure are different facts.
- DOM effects belong in effects, never per render; promise order must be guaranteed.
- Look for guards that control flow already proves, and machinery that can never fire (an abort controller nothing aborts).

**Tests**
- Assertions check the specific behavior, fixtures create the claimed scenario, and the data includes rows that would fail if filtering were wrong. A test that only checks a mock returns what it was told proves nothing.
- Save-path changes get a real round trip: fetch, change, save, reload, assert.
- Do not repeat what the shared test config already does; use one of fireEvent or userEvent, not both.

**Performance and scale**
- Providers, locations, and concepts run to thousands in production. Dropdowns over full lists, eager loads, and per-item requests that pass on demo data are the usual problems.
- Count the requests a feature makes: waterfalls, duplicate fetches, serial awaits in loops.

**Frontend quality**
- Error text is for the user; the affordance matches recoverability (toast for unrecoverable, inline for actionable).
- Strings that only parse in English get restructured; placeholder values are translated too; non-English translations come through Transifex.
- ARIA must be true to the widget; wrong ARIA is worse than none. Check keyboard use and focus.
- Every data-driven component has loading, empty, error, and misconfigured states. A feature that disappears on a failed fetch looks unconfigured.
- Use the Carbon component built for the interaction (IconButton, MultiSelect, one primary button per view).
- Styles stay inside the component; spacing uses design tokens. Do not flag hardcoded colors on theming grounds; O3 does not use theme tokens.

**Ecosystem and process**
- Does the platform already provide this, and does the change undo or solve again something an earlier fix handled?
- A pom scope change, a new config.xml requirement, or a soft dependency changes what deployments must run; optional modules stay optional.
- No hard-coded keys or secrets.
- For code that spans core versions, prefer a runtime version check with graceful degradation.
- New patterns land beside the old ones; refactors of existing consumers come in follow-up PRs. A replacement component needs a feature-parity check.
- Incidental changes ride separately; a breaking or config-renaming change does not ship under `fix`.
- A contributor's "it breaks tests" is an unverified claim too.
- A non-obvious value or behavior needs an inline comment saying why.
- Unused imports, files, classes, and config properties get deleted.

## Backend Checks

Decide any claim about backend behavior by the mechanism that determines it, not by the frontend's TS types.

1. State the claim as the smallest checkable assertion: "the `default` representation includes field X", "this property is creatable through POST", "the search supports filter Z".
2. Find the handler. The frontend calls `/ws/rest/v1/<resource>`; grep for the `@Resource(` annotation with that path segment, usually under `omod/src/main/java/.../web/resources/`. Core and standard resources live in `openmrs-module-webservices.rest`, FHIR in `openmrs-module-fhir2`, domain resources in their owning module (queue endpoints in `openmrs-module-queue`). Clone the module blobless under ~/repos/.
3. Read the handler in full: `getRepresentationDescription` for the fields per `DEFAULT`, `FULL`, and `REF`; `getCreatableProperties` and `getUpdatableProperties`; `@PropertyGetter` and `@PropertySetter`; `doSearch` and `doGetAll`; the validation path. A field missing from the TS type can still ship.
4. When code reading leaves doubt, call dev3 with the request the frontend sends (same parameters and representation). Make a session cookie first, because basic auth on each request can fail: `curl -s -c ~/omrs.jar -u admin:Admin123 https://dev3.openmrs.org/openmrs/ws/rest/v1/session -o /dev/null`, then `curl -s -b ~/omrs.jar 'https://dev3.openmrs.org/openmrs/ws/rest/v1/...'`. Cut error bodies with `| head -c 500`.
5. Trust, in order: the live response for the exact representation, then the handler read in full, then the TS type (only for what the frontend expects). Keep confirmed facts and inferences apart.

dev3 runs the current development builds of the modules, not this PR. For a form-schema claim, check the React engine (`openmrs-esm-form-engine-lib`), not the Angular one.

## Release Coordination Notes

Some PRs are correct only when a paired module, distro, or backend PR lands first. Check the paired PR before you treat the dependency as safe. Post merge order or version needs as a non-blocking note unless the PR breaks currently supported environments by itself. Do not present coordination as a code defect.

## OpenMRS Defaults

- OpenMRS repos squash-merge. Do not ask contributors to clean up branch history.
- Do not use `refactor` as a conventional commit label.
- No em dashes in review text.

## Comment Voice

Comments read like an experienced O3 maintainer: short, warm, specific. Almost every comment is shorter than you think.

**With a suggestion block,** the block is the explanation. The prose is one line, usually a sentence fragment naming the purpose or the defect. Complete examples:

- "To expose the open/closed state for screen readers" + block
- "This previously disabled all privileges" + block

When the defect is not obvious from the block, add one cause-and-effect sentence: "Looks like this visits-page rep doesn't include `certainty` and because of that the tag never shows."

**Without a suggestion block,** one or two sentences: the fact, then the ask. "None of the mocks have `form.resource`, so `encounterHasJsonSchemaForm` is always false, so the print and embedded form part doesn't happen. Lets add a fixture with a [JSON schema resource](url#L127-L132) and assert that the slot receives `mode: 'embedded-view'`."

**Questions and design concerns** may run longer, walking the author through the change, and still end with a concrete ask: "So previously, the encounters table used to fetch the encounter from the visit, now we take them from the prop, but that means that whoever passes in the visit needs to have fetched the visit with the encounter. For example, does the service queues pass it as such? We might want to add that as a comment."

**Cut these before you post:**
- Your verification evidence ("I tried swapping the gate and the test still passed"). It goes in "What I checked", not in a comment.
- How the code got this way.
- Arguments against a fix nobody proposed.
- Severity framing ("This is the main thing I'd like to see before this lands"). The body sizes the ask.
- A second corroborating detail. One reason is enough.
- Self-justification ("I just want to be sure...").
- Consequence chains. One cause-and-effect sentence at most.

**Every comment carries the fix.** No "we should introduce a config for this" or "worth a grep before this lands": name the key with its `_default` and description, run the grep and give the result, list the call sites. If the fix is a real choice between two options, state both and say which you would take.

- Use only names that exist in the code or on the screen. Never a shorthand coined during the review, and never "this" as the subject of a sentence.
- Every comment that asks for a code change carries a suggestion block, except dead code ("This is no longer used") and edits that are not next to each other (a plain fenced block naming the files).
- Pick the ask form by the kind of finding. Test gaps and design asks use a question: "Can we add a test that covers...", "Shouldn't we show...". A clear defect uses "Let's <verb>": "Let's move this to the `useMemo` so we only do this when the encounters actually change." Settled conventions get a flat imperative: "These validation errors must be translated." Hedges ("Probably better to...") only for things the author may decline.
- Link inline, on the words that make the claim, with a real URL pinned to a commit and a `#Lxx-Lyy` range: `[backend doesn't actually parse](https://github.com/<org>/<repo>/blob/<sha>/<path>#L37-L44)`. At most one link per comment; most comments need none.
- When a finding spans two lines, post two comments: the fact on the line that causes it, the suggestion on the line that changes, pointing back with "^" ("because of the comment ^ about the hook hardcoding the waiting status concept name"). Compress repeats with "Ditto" or "Same as above".
- Ask for the change; do not diagnose the gap. "Lets add a test case that covers a scenario where both timestamps are missing" beats "the guard skips an entry when either timestamp is missing, but we only cover the missing `endedAt` side here".
- Nits teach the general rule: "Things like this should really be outside of the render function, so this runs for every encounter on every render, which we don't really need." Never attach "feel free to ignore" to something that is wrong.
- `Nit:` on its own line, used only when the comment would otherwise read as a change request (a docs or config-description fix). Most nits carry no label.
- Suggestion blocks append; they do not reorder. Do not sort props or imports or reformat code around your change.
- Say the ask in the words of the code: "Should we drop the try/catch?" beats a paragraph on exception semantics. "Won't merging a patient with an active queue entry fail here? Core moves the visits before it calls `voidPatient`, so the save triggers the `visit.wrongPatient` rule."
- Words not to use: pin, pinned, pins; hoist; thread (except a GitHub thread); branch (for a code path); lookup; business rule; on main; workflow order; inclusive bound; structural (for types). Say what the code does instead.
- No em dashes. No headers, numbered bold findings, "What breaks:", or "Fix:" sections inside a comment.
- Emoji are rare: at most a `:)` to soften a single request.
- Do not tag maintainers or route a finding to a person. Resolve it from the code, or ask the author.
- Never open an inline comment with its own importance.

More examples of the register:

- "Wouldn't doing `formList[0].uuid` break in the (maybe unlikely) scenario that formList is an empty array?"
- "Can't we use the type `Encounter` for this, instead of using `any`?"
- "Since we only want the count, why aren't we using `useQueueEntriesMetrics` here?"
- "We should assert that the value is correct here, and not just the path"
- "We should reset the selected service here as well, right?"
- "The modal lets you submit location "All" with a service picked." + block
- "In the current visit component, we passed the role directly to the skeleton" + block
- "Can we add a test for the resource file to test out the filter? E.g - `excludes the current visit when it started before today` and `returns the most recent past visit when it is not the current visit`"
- "This is an override for carbon styling so the classname shouldn't be changed"
- "Looks like some changes have gotten reverted to a previous state, could you please update your branch and check again?"
- A file-level point anchored on line 1, as a plain fenced list: "There's a couple of orphaned configs left:" followed by the keys.
- A deletion: "Duplicate" with an empty suggestion block.

## Review Body

The review is always posted as COMMENT. The body opens with a greeting by @handle and the size of the ask, counted by kind ("Thanks @author! A couple of suggestions and a blocker -", "Thanks @author! Just one suggestion -"). With no findings, it opens "Thanks @author! No changes to ask for." A symptom you saw while running the app but could not trace to a line (a crash on an interaction, drifted spacing) goes right after the greeting: describe what you saw, hedge the cause, and ask for something concrete. Never preview the inline comments in the body.

Then, in plain words, at most 40 lines:

````
**What it changes**
<up to 3 lines, plain words, no identifiers>

**How to test it**
1. <steps on dev3 or locally, with the exact screen and data>

**What I checked**
| Check | Result | Refs |
| --- | --- | --- |
<one row each: lint, type check, tests run, app run, backend checks, searches; the commit you checked>

**Not covered:** <one line: what you did not check and why, including "diff only" when the change was not safe to build>
<then one bullet per problem you found that this PR did not introduce: "Not from this PR: <file:line> <what is wrong, one sentence>". Leave out the bullets when there are none.>

**Findings at a glance**
| # | File:line | Severity | What is wrong | Fix |
| --- | --- | --- | --- | --- |
````

- Number findings `<file number>.<finding number>`, with files in the order GitHub's Files changed tab shows them. Order the table by file, never by severity.
- Severity tags: `blocker`, `non-blocking`, `nit`, `question`, `test gap`.
- One short plain idea per cell, no code quotes.
- Omit the findings table when there are no findings, and put the evidence in "What I checked".
- Name the commit you checked in "What I checked".
- Inline comments carry the detail; the table is an index.

PR-REVIEW-SKILL-MARKER
