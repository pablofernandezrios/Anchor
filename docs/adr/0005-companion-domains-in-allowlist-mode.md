# ADR 5: an allowed site brings the domains it cannot work without

**Status:** accepted, 2 October 2026, by the owner.

## The problem

The owner put `github.com` on an allowlist profile and got a column of
unstyled links. GitHub keeps its stylesheets, scripts and images on
`github.githubassets.com` and `avatars.githubusercontent.com`, which are
different registrable domains and therefore not covered by the rule. The
same happens with almost every site: the page comes from one domain and what
the page is made of comes from several others.

His request was that Anchor do it by itself: "when I am inside one of my
allowed sites, that page should load everything it needs".

## Why it cannot be inferred

Anchor blocks at the DNS layer, and a DNS query is a name and nothing else.
It carries no referrer, no origin, no tab, no process. A lookup for
`github.githubassets.com` is byte-identical whether it came from the GitHub
tab the user is reading or from anything else on the machine. "What the
allowed page needs" is not information Anchor has.

Only something inside the browser knows which page asked for what, and that
is a different architecture — a browser extension, per browser, with its own
install story and its own trust problem. SPEC 2.2 and ADR 1 already ruled
that out for the blocking itself.

So the choice was between knowing in advance and not doing it at all.

## The decision

Two shipped lists, both editable, both left alone by package upgrades, in
the same spirit as `essentials.txt`.

`web-assets.txt` is shared infrastructure: font services, script
repositories, the large content networks. It is exempt **in allowlist mode
only**.

`companions.txt` maps a site to the domains it cannot work without:
`github.com` brings `githubassets.com` and `githubusercontent.com`. A
companion applies **only when its site is on the allowlist**. Allowing
nothing brings nothing.

Companions do not have companions. One hop, because a chain would be a quiet
way to allow a great deal by naming one thing.

## Why this is a smaller concession than it looks

In allowlist mode a person reaches a site by its name. Allowing
`fastly.net` does not make `reddit.com` reachable, because `reddit.com`
still does not resolve. Content networks are where pages keep their
furniture; they are not destinations anybody navigates to.

The asset list is not in force in blocklist mode. There the user named what
to block, and a list Anchor ships must never quietly undo that. The
companion list is allowlist-only by construction, since in blocklist mode
there is no allowlist to extend.

## What it costs

It is best effort and always will be. A site nobody has written down still
arrives stripped, and the fix is a line in a file. `anchor stats` names what
was refused, so finding that line takes a minute rather than a guess.

It is also a maintenance treadmill: sites move their assets, and the list
goes stale. The mitigation is that it is a plain text file the user owns,
not a compiled-in table — a stale entry costs one site, and anyone can fix
it without waiting for a release.

## What was rejected

**A name heuristic** — allowing anything that looks like the site, so
`github.com` would permit `githubassets.com` by shared prefix. It fails in
both directions: `fbcdn.net` shares nothing with `facebook.com`, and a rule
keyed on substrings would let `x.com` permit most of the internet.

**A learning pass**, where a first session records instead of blocking and
then offers what it saw. A better answer in principle, and the owner may
still want it; rejected for now only because the lists work on day one
without the user doing anything, which is what he asked for.
