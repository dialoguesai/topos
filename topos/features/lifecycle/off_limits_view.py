"""Which Off-limits entries a read sees: decided in one place, from what the node itself can verify.

The upgrade to 1.5.0 carries the owner's older per-person "exclude" choices into the Off-limits list by itself. An
exclude was a choice about sharing, so until the owner acts on such an entry it is CARRIED AND WAITING
(`blackhole.WAITING_COLUMN`), and the rule is (the third fix round, ruling P, 7 Oct 2026):

  - every path whose answer can leave the node toward ANOTHER PERSON sees it, at once (`EVERYONE`);
  - every path that serves THE OWNER HIMSELF does not see it (`OWNER`): nothing dropped, nothing marked protected,
    no model call moved, no summary withheld on its account.

`EVERYONE` is the default of every reader in the node, so a path nobody listed keeps reading every entry, exactly
as it did before there were two views. A reader takes the `OWNER` view only by asking one of the three functions
below, each of which says who it is for. The share side (`permissions_v2`) asks none of them and reads the table
itself: every entry, always.

Who is "the owner himself", for a request, is positive evidence at the node's own door and nothing else:

  - the owner's app: the channel verified `owner_app` (the local socket, or the control plane's signed stamp);
  - his own outside client under his own key: a client that authenticated at this node's own HTTP door with one of
    the node's keys, or a verified `third_party` stamp from the control plane that names THIS node's owner. The
    owner is looked up here, again, and is not taken from the dispatcher having let the frame through: a node that
    cannot say who its owner is treats every relayed third party as someone else.

Everything else keeps reading every entry: a relayed third party who is not the owner, a frame with no stamp, a
request with no principal, and THE ROUTINE LANE (`ROUTINE_LANE`). A routine's result goes to the owner and, when the
routine lists consented recipients, is also mailed to other people, and nothing on a routine's frame says which
(control plane `routines_executor`, `routines_engine_bridge`: the stamp is `owner_automation` either way). The
node cannot serve "his own routines to him" unchanged without also changing "routine mail addressed to anyone
else", so the lane stays as it was, the side that protects, until the control plane can say on the frame that a
run's output is addressed to the owner alone.
"""

from __future__ import annotations

from typing import Any, Optional

from .blackhole import EVERYONE, OWNER

#: The routine lane's view, in one named place (see the module docstring). EVERYONE: a carried, waiting entry is
#: still withheld from every routine, the owner's own included. Changing this word serves the owner's routines as
#: before the upgrade and lets a carried person into routine mail addressed to other people: a decision, not a fix.
ROUTINE_LANE = EVERYONE

_ROUTINE_CLASS = "owner_automation"
_OWN_DOOR_CHANNELS = frozenset({"local_http", "remote_http", "uds"})


def _relay_owner() -> Optional[str]:
    """This node's owner as the relay dispatcher reads it (`core.handlers._relay_owner_id`): the bound identity's
    owner, else the engine config's user id; None when the node cannot say."""
    try:
        from ...core.handlers import _relay_owner_id

        return _relay_owner_id()
    except Exception:  # noqa: BLE001 -- unknown is never the owner
        return None


def is_owner_himself(principal: Any) -> bool:
    """Whether the node itself can tell that what it returns under this channel-verified principal goes to its
    owner (the module docstring's two cases). False for everything it cannot place."""
    from ...principal import OWNER_APP, THIRD_PARTY

    cls = getattr(principal, "cls", None)
    if cls == OWNER_APP:
        return True
    if cls != THIRD_PARTY:
        return False
    channel = getattr(principal, "channel", None)
    if channel in _OWN_DOOR_CHANNELS:
        return True                       # one of this node's own keys, checked at this node's own door
    if channel != "cp_relay":
        return False
    acting = getattr(principal, "acting_user", "") or ""
    owner = _relay_owner()
    return bool(acting) and owner is not None and acting == owner


def is_another_person(principal: Any) -> bool:
    """Whether this principal is positively someone the node cannot take for its owner: a relayed third party who
    does not name this node's owner (a recipient, or a stamp that named nobody)."""
    from ...principal import THIRD_PARTY

    return (getattr(principal, "cls", None) == THIRD_PARTY and getattr(principal, "channel", None) == "cp_relay"
            and not is_owner_himself(principal))


def for_request(principal: Any = None, *, current: bool = True) -> str:
    """The view of a read made to ANSWER A REQUEST (the query pipeline, the owner's read routes).

    OWNER only for the owner himself (`is_owner_himself`). The routine lane is `ROUTINE_LANE`. Every other caller,
    and a request with no principal, reads every entry. With `current`, a missing principal is taken from the
    request's own context."""
    if principal is None and current:
        from ...principal import current_principal

        principal = current_principal()
    if getattr(principal, "cls", None) == _ROUTINE_CLASS:
        return ROUTINE_LANE
    return OWNER if is_owner_himself(principal) else EVERYONE


def for_own_processing(principal: Any = None, *, current: bool = True) -> str:
    """The view of WORK THE NODE DOES ON THE OWNER'S OWN DATA, whose result is not an answer to anyone: the model
    gate (where the owner's text may be processed) and the producers of his own derived text and graph.

    Such work mostly has no request behind it, so "no principal" cannot mean "someone else" here. It is OWNER unless
    the work is running for a caller who is positively another person (`is_another_person`): the share doors set
    their recipient as the principal for the length of a read, so if a share's work ever reached one of these
    paths it would read every entry."""
    if principal is None and current:
        from ...principal import current_principal

        principal = current_principal()
    return EVERYONE if is_another_person(principal) else OWNER
