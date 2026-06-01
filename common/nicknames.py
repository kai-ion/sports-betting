"""Player nickname → real name mapping for ESPN lookups."""

NICKNAMES = {
    # NBA
    "Deuce McBride": "Miles McBride",
    "PJ Washington": "P.J. Washington",
    "OG Anunoby": "O.G. Anunoby",
    "Nic Claxton": "Nicolas Claxton",
    "Lu Dort": "Luguentz Dort",
    "Herb Jones": "Herbert Jones",
    "RJ Barrett": "R.J. Barrett",
    "AJ Griffin": "A.J. Griffin",
    "TJ McConnell": "T.J. McConnell",
    "CJ McCollum": "C.J. McCollum",
    "JJ Redick": "J.J. Redick",
    "SGA": "Shai Gilgeous-Alexander",
    "KAT": "Karl-Anthony Towns",
    # WNBA
    "A'ja Wilson": "A'ja Wilson",
    "CC": "Caitlin Clark",
}


def resolve_name(name):
    """Return the ESPN-compatible name for a player."""
    return NICKNAMES.get(name, name)
