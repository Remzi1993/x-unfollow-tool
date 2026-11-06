"""Unfollow from an existing X Following tab in Chrome."""

import argparse
import re
import sqlite3
import sys
import time
from datetime import datetime
from urllib.parse import urlsplit

from playwright.sync_api import Browser, Error, Locator, Page, Response, expect, sync_playwright

from pacing import Limits, Pacer


HANDLE = re.compile(r"[a-z0-9_]{1,15}", re.IGNORECASE | re.ASCII)
UNFOLLOW_BUTTON = re.compile(r"^.+-unfollow$")
FOLLOWING_LABEL = re.compile(r"^(?:Following|Unfollow) @([a-z0-9_]{1,15})$", re.I)
EMPTY_FOLLOWING = re.compile(r"^you(?: are not|['’]re not| aren['’]t) following anyone(?: yet)?[.!]?$", re.I)
RESTRICTION = re.compile(
    r"rate limit|too many requests|try again later|account (?:is |has been )?(?:locked|restricted|suspended)"
    r"|verify (?:your identity|your account|you are human)",
    re.I,
)
UI_TIMEOUT = 8_000


def is_following_url(url: str, handle: str) -> bool:
    parsed = urlsplit(url)
    return (
        parsed.scheme == "https"
        and parsed.netloc == "x.com"
        and parsed.path.rstrip("/").lower() == f"/{handle}/following"
    )


def find_tab(browser: Browser, handle: str) -> Page:
    pages = [
        page for context in browser.contexts for page in context.pages
        if is_following_url(page.url, handle)
    ]
    if len(pages) != 1:
        raise RuntimeError(f"Open exactly one Chrome tab at https://x.com/{handle}/following")
    return pages[0]


def is_unfollow_response(response: Response) -> bool:
    url = urlsplit(response.url)
    return (
        url.hostname in {"x.com", "api.x.com"}
        and response.request.method == "POST"
        and url.path.endswith(("/friendships/destroy.json", "/Unfollow", "/UnfollowUser"))
    )


class FollowingTab:
    def __init__(self, page: Page, handle: str) -> None:
        self.page = page
        self.handle = handle
        navigation = page.get_by_role("navigation", name="Primary", exact=True)
        self.profile_link = page.get_by_test_id("AppTabBar_Profile_Link").or_(
            navigation.get_by_role("link", name="Profile", exact=True)
        ).filter(visible=True)
        legacy_column = page.get_by_test_id("primaryColumn").filter(visible=True)
        self.column = legacy_column if legacy_column.count() == 1 else page.get_by_role("main")
        self.cells = self.column.get_by_test_id("UserCell")
        self.buttons = self.cells.get_by_test_id(UNFOLLOW_BUTTON).or_(
            self.column.get_by_role("button", name=FOLLOWING_LABEL)
        ).filter(visible=True)
        self.dialogs = page.locator(
            'dialog[open], [role="dialog"], [role="alertdialog"], [data-testid="confirmationSheetDialog"]'
        ).filter(visible=True)
        self.stop_reason = None
        page.on("response", self.check_response)

    def check_response(self, response: Response) -> None:
        if response.status == 429 and urlsplit(response.url).hostname in {"x.com", "api.x.com"}:
            self.stop_reason = "X returned HTTP 429. Review the account before restarting."

    def check_page(self) -> None:
        if self.stop_reason:
            raise RuntimeError(self.stop_reason)
        if not is_following_url(self.page.url, self.handle):
            raise RuntimeError("The selected tab left the Following page.")
        profile_href = self.profile_link.get_attribute("href", timeout=UI_TIMEOUT) or ""
        if profile_href.rstrip("/").lower() != f"/{self.handle}":
            raise RuntimeError(f"The selected tab is not signed in as @{self.handle}.")
        warnings = self.page.locator(
            '[data-testid="toast"], [role="alert"], [role="alertdialog"], [role="status"]'
        )
        if warnings.filter(has_text=RESTRICTION, visible=True).count():
            raise RuntimeError("X displayed a rate-limit or account restriction message.")

    def check_ready(self) -> None:
        self.check_page()
        if self.dialogs.count():
            raise RuntimeError("Dismiss the existing confirmation dialog in the Following tab.")

    def next_button(self) -> Locator | None:
        """Find a rendered unfollow button or confirm the list is empty."""
        unchanged = 0
        previous_position = None
        for _ in range(50):
            self.check_ready()
            if self.buttons.count():
                return self.buttons.first
            if self.column.get_by_text(EMPTY_FOLLOWING).filter(visible=True).count():
                return None

            position = self.page.evaluate("[window.scrollY, document.scrollingElement.scrollHeight]")
            unchanged = unchanged + 1 if position == previous_position else 0
            if unchanged >= 5:
                break
            previous_position = position
            self.page.evaluate('window.scrollBy({top: window.innerHeight * 0.85, behavior: "instant"})')
            self.page.wait_for_timeout(1_000)

        raise RuntimeError("No unfollow controls or explicit empty-list message found. Check the tab.")

    def identify_target(self, candidate: Locator) -> tuple[Locator, str]:
        label = candidate.get_attribute("aria-label", timeout=UI_TIMEOUT) or ""
        match = FOLLOWING_LABEL.fullmatch(label)
        if match and HANDLE.fullmatch(match[1]):
            target = match[1].lower()
            target_label = re.compile(rf"^(?:Following|Unfollow) @{re.escape(target)}$", re.I)
            return self.column.get_by_role("button", name=target_label).filter(visible=True), target

        test_id = candidate.get_attribute("data-testid", timeout=UI_TIMEOUT)
        if not test_id or not UNFOLLOW_BUTTON.fullmatch(test_id):
            raise RuntimeError("The candidate no longer has an unfollow control.")

        row = self.cells.filter(has=self.page.get_by_test_id(test_id))
        profile_paths = row.locator('a[href^="/"]').evaluate_all(
            "links => links.map(link => link.getAttribute('href'))"
        )
        profiles = {path[1:] for path in profile_paths if HANDLE.fullmatch(path[1:])}
        profiles = {name.lower() for name in profiles if row.get_by_text(f"@{name}", exact=True).count()}
        if len(profiles) != 1:
            raise RuntimeError("Could not verify the account shown in the target row.")
        return self.cells.get_by_test_id(test_id).filter(visible=True), profiles.pop()

    def unfollow(self, candidate: Locator, pacer: Pacer) -> str:
        button, target = self.identify_target(candidate)

        self.check_ready()
        # Dispatch to the verified control even when Chrome throttles background rendering.
        button.dispatch_event("click", timeout=UI_TIMEOUT)
        title = re.compile(rf"^Unfollow @{re.escape(target)}\?$", re.I)
        dialog = self.dialogs.filter(has=self.page.get_by_text(title))
        confirm = dialog.get_by_role("button", name="Unfollow", exact=True).or_(
            dialog.get_by_test_id("confirmationSheetConfirm")
        ).filter(
            has_text=re.compile(r"^\s*Unfollow\s*$"), visible=True
        )
        expect(confirm).to_be_visible(timeout=UI_TIMEOUT)
        self.check_page()
        if pacer.reserve() is not None:
            raise RuntimeError("The pacing budget changed. Dismiss the dialog and restart the tool.")

        try:
            with self.page.expect_response(is_unfollow_response, timeout=UI_TIMEOUT) as pending:
                confirm.dispatch_event("click", timeout=UI_TIMEOUT)
            response = pending.value
            if not response.ok:
                raise RuntimeError(f"X rejected the unfollow request (HTTP {response.status}).")
            expect(dialog).to_have_count(0, timeout=UI_TIMEOUT)
            expect(button).to_have_count(0, timeout=UI_TIMEOUT)
        except (AssertionError, Error, RuntimeError):
            self.check_page()
            raise
        self.check_page()
        return target


def wait_for_budget(tab: FollowingTab, pacer: Pacer) -> None:
    eligible_at = pacer.next_at()
    remaining = eligible_at - time.time()
    if remaining > 0:
        resume_at = datetime.fromtimestamp(eligible_at).astimezone().isoformat(timespec="seconds")
        print(f"Waiting until {resume_at}", flush=True)
    while remaining > 0:
        tab.page.wait_for_timeout(min(remaining, 30) * 1_000)
        tab.check_ready()
        remaining = pacer.next_at() - time.time()


def run(tab: FollowingTab, pacer: Pacer, max_actions: int | None) -> None:
    tab.check_ready()
    tab.page.evaluate('window.scrollTo({top: 0, behavior: "instant"})')
    completed = 0
    while max_actions is None or completed < max_actions:
        button = tab.next_button()
        if button is None:
            print(f"X shows an empty Following list. Unfollowed {completed} in this run.", flush=True)
            return
        wait_for_budget(tab, pacer)
        target = tab.unfollow(button, pacer)
        completed += 1
        print(f"Unfollowed @{target} ({completed} this run)", flush=True)
    print(f"Stopped at the requested run limit of {max_actions}.", flush=True)


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("handle", help="Your signed-in X handle")
    parser.add_argument("--apply", action="store_true", help="Unfollow accounts. Default is a read-only preview.")
    parser.add_argument("--max-actions", type=int, help="Stop after this many successful unfollows")
    args = parser.parse_args()
    handle = args.handle.removeprefix("@").lower()
    if not HANDLE.fullmatch(handle):
        parser.error("handle must contain 1–15 letters, digits, or underscores")
    if args.max_actions is not None and (not args.apply or args.max_actions < 1):
        parser.error("--max-actions requires --apply and a positive integer")

    try:
        limits = Limits.from_env()
        with sync_playwright() as playwright:
            try:
                browser = playwright.chromium.connect_over_cdp("chrome", no_defaults=True, timeout=60_000)
            except Error as exc:
                raise RuntimeError(
                    "Could not attach to Chrome. Enable remote debugging at "
                    "chrome://inspect/#remote-debugging and allow the connection prompt."
                ) from exc
            tab = FollowingTab(find_tab(browser, handle), handle)
            tab.check_ready()
            tab.column.wait_for(state="visible", timeout=UI_TIMEOUT)
            if args.apply:
                run(tab, Pacer(handle, limits), args.max_actions)
            else:
                print(f"Preview for @{handle}. Found {tab.buttons.count()} rendered unfollow controls.")
        return 0
    except KeyboardInterrupt:
        print("Stopped by user.", file=sys.stderr)
        return 130
    except (AssertionError, Error, OSError, RuntimeError, ValueError, sqlite3.Error) as exc:
        print(f"Stopped. {exc}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    sys.exit(main())
