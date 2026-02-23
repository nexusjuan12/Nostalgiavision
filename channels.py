"""Predefined channel configurations — ported from ChannelRepository.java."""

from dataclasses import dataclass, field
from typing import Optional


@dataclass
class TimeSlotRule:
    show_rating_key: str
    show_title: str
    start_hour: int
    end_hour: int
    days_of_week: set = field(default_factory=set)  # 0=Mon .. 6=Sun; empty=all


@dataclass
class DayOfWeekRule:
    show_rating_key: str
    show_title: str
    allowed_days: set = field(default_factory=set)  # 0=Mon .. 6=Sun; empty=all


@dataclass
class AdvancedScheduleConfig:
    time_slot_rules: list = field(default_factory=list)   # list[TimeSlotRule]
    day_of_week_rules: list = field(default_factory=list) # list[DayOfWeekRule]
    premiere_enabled: bool = False
    premiere_time_hour: Optional[int] = None


# Sorting methods — mirror SortingMethod.java enum
RANDOM = "RANDOM"
CYCLIC_SHUFFLE = "CYCLIC_SHUFFLE"
BLOCK_SHUFFLE = "BLOCK_SHUFFLE"
BLOCK_CYCLIC = "BLOCK_CYCLIC"


@dataclass
class ChannelConfig:
    number: int
    name: str
    genres: list = field(default_factory=list)
    studios: list = field(default_factory=list)
    content_ratings: list = field(default_factory=list)
    keywords: list = field(default_factory=list)
    collection_ids: list = field(default_factory=list)
    collection_names: list = field(default_factory=list)
    type: str = "mixed"               # movie | series | mixed | music | weather
    max_year: Optional[int] = None
    max_movies_per_day: Optional[int] = None
    sorting_method: str = RANDOM
    marathons_enabled: bool = False
    mature_content_after_hour: Optional[int] = None
    advanced_config: Optional[AdvancedScheduleConfig] = None
    is_custom: bool = False
    custom_logo_path: Optional[str] = None
    custom_media_rating_keys: list = field(default_factory=list)
    display_number: Optional[int] = None
    excluded_rating_keys: list = field(default_factory=list)


def _ch(number, name, **kwargs) -> ChannelConfig:
    return ChannelConfig(number=number, name=name, **kwargs)


# ── Predefined channels (ported from ChannelRepository.java) ──────────────────

PREDEFINED_CHANNELS: list[ChannelConfig] = [
    # Disney channels
    _ch(1001, "Dizzy Channel",       genres=["Animation", "Family"], studios=["Disney"], type="mixed"),
    _ch(1002, "Dizzy Junior",        genres=["Animation", "Children"], studios=["Disney"], keywords=["Junior", "Mickey"], type="series"),
    _ch(1003, "DizzyXD",             genres=["Animation", "Action", "Comedy"], studios=["Disney"], type="series"),
    _ch(1004, "Toon Dizzy",          genres=["Animation"], studios=["Disney"], type="mixed", max_year=1999, max_movies_per_day=2),
    _ch(1005, "Playhome Dizzy",      genres=["Children", "Animation"], keywords=["Pooh", "Bear", "Playhouse"], type="series"),

    # Cartoon / animation
    _ch(1006, "Cartoon Net",         genres=["Animation"], studios=["Cartoon Network"], type="mixed", max_movies_per_day=2),
    _ch(1007, "Boomer-Rang",         genres=["Animation"], studios=["Hanna-Barbera", "Warner Bros"], keywords=["Scooby", "Looney", "Tom and Jerry"], type="mixed", max_movies_per_day=2),
    _ch(1008, "Pennyodeon",          genres=["Animation", "Family", "Children"], studios=["Nickelodeon"], type="mixed"),
    _ch(1009, "Penny Jr.",           genres=["Children"], studios=["Nickelodeon"], keywords=["Dora", "Blue", "Patrol"], type="series"),
    _ch(1010, "P.B.Yes Tots",        genres=["Children", "Family"], studios=["PBS"], type="series"),

    # Lifestyle / food
    _ch(1011, "Meal Network",        genres=["Reality", "Food", "Cooking"], studios=["Food Network"], type="series"),
    _ch(1012, "TV World",            genres=["Comedy", "Sitcom"], content_ratings=["TV-PG", "TV-G"], type="series"),

    # Drama / action
    _ch(1013, "EF-X",               genres=["Action", "Thriller", "Drama", "Crime"], studios=["FX"], type="mixed"),
    _ch(1014, "EF-XX",              genres=["Comedy"], studios=["FX", "FXX"], type="mixed"),
    _ch(1015, "eon Television",      genres=["Drama", "Crime"], keywords=["Law", "Order", "Criminal"], type="series"),
    _ch(1016, "Story Channel",       genres=["Documentary", "History"], studios=["History"], type="mixed"),

    # Broadcast networks
    _ch(1017, "N.B.Sea",            studios=["NBC"], type="mixed"),
    _ch(1018, "A.B.Sea",            studios=["ABC"], type="mixed"),
    _ch(1019, "NTV",                genres=["Reality"], studios=["MTV"], type="mixed"),
    _ch(1020, "SeaW",               studios=["The CW"], type="mixed"),
    _ch(1021, "C.B.Yes",            studios=["CBS"], type="mixed"),

    # Major cable
    _ch(1022, "FAUX",               genres=["Animation", "Comedy", "Drama", "Thriller", "Action", "Reality"], studios=["Fox"], type="mixed"),
    _ch(1023, "H.G.T.Vee",          genres=["Reality", "Home", "Garden"], studios=["HGTV"], type="series"),
    _ch(1024, "A.M.Sea",            genres=["Drama", "Action", "Thriller"], studios=["AMC"], type="mixed"),
    _ch(1025, "B.B.Sea",            studios=["BBC"], type="mixed"),
    _ch(1026, "T.N.Tea",            genres=["Action", "Thriller", "Drama"], studios=["TNT"], type="mixed"),
    _ch(1027, "T.L.Sea",            genres=["Reality"], studios=["TLC"], type="series"),

    # Specialty
    _ch(1028, "Trademark Channel",  genres=["Romance"], studios=["Hallmark"], type="movie"),
    _ch(1029, "Uncover Channel",    genres=["Documentary", "Science"], studios=["Discovery"], type="series"),
    _ch(1030, "Animal Globe",       genres=["Documentary", "Animals", "Nature"], studios=["Animal Planet"], type="series"),
    _ch(1031, "YouTV",              genres=["Comedy", "Sitcom", "Classic"], type="series"),
    _ch(1032, "National Geography", genres=["Documentary", "Nature"], studios=["National Geographic"], type="mixed"),
    _ch(1033, "Sigh-Fi",            genres=["Sci-Fi", "Science Fiction", "Fantasy"], studios=["Syfy"], keywords=["Harry Potter"], type="mixed"),
    _ch(1034, "Terror Channel",     genres=["Horror"], type="movie"),

    # Comedy / entertainment
    _ch(1035, "Comedy Middle",      genres=["Comedy"], studios=["Comedy Central"], type="mixed"),
    _ch(1036, "M.G.N.",             type="movie", max_year=1989),
    _ch(1037, "Watch-On-Repeat",    genres=["Comedy", "Sitcom"], keywords=["Office", "Friends", "Seinfeld"], type="series"),
    _ch(1038, "Nap @ Nite",         genres=["Comedy", "Sitcom"], type="series"),
    _ch(1039, "TeeBS",              genres=["Comedy", "Sitcom"], studios=["TBS"], type="mixed"),
    _ch(1040, "Spoke",              genres=["Reality", "Action"], studios=["Spike"], type="mixed"),
    _ch(1041, "TruthTV",            genres=["Reality", "Comedy"], studios=["truTV"], type="mixed"),
    _ch(1042, "US Yay",             genres=["Drama", "Comedy", "Crime"], studios=["USA Network"], type="mixed"),
    _ch(1043, "A&Me",               genres=["Reality", "Crime", "Drama"], studios=["A&E"], type="mixed"),
    _ch(1044, "GuessSN",            genres=["Game Show"], studios=["GSN"], type="series"),
    _ch(1045, "TCN",                type="movie", max_year=1989),
    _ch(1046, "Adult Skim",         genres=["Animation", "Comedy"], studios=["Adult Swim"], type="series"),

    # Holiday / special interest
    _ch(1047, "Holiday Channel",    genres=["Holiday", "Christmas", "Family"], keywords=["Christmas", "Holiday", "Santa"], type="movie"),
    _ch(1048, "VHS Channel",        genres=["Action", "Horror", "Sci-Fi"], keywords=["80s", "90s"], type="movie"),
    _ch(1049, "SPARZ",              genres=["Action", "Drama", "Sci-Fi", "Adventure"], type="movie"),
    _ch(1050, "CINEMIN",            genres=["Action", "Drama", "Thriller", "Adventure"], type="movie"),

    # Streaming services
    _ch(1051, "Feeform",            studios=["Freeform", "ABC Family"], type="mixed"),
    _ch(1052, "Lifetune",           studios=["Lifetime"], type="mixed"),
    _ch(1053, "Dizzy+",             studios=["Disney"], type="mixed"),
    _ch(1054, "Netflicks",          studios=["Netflix"], type="mixed"),
    _ch(1055, "Pear TV+",           studios=["Apple"], type="mixed"),
    _ch(1056, "Hula",               studios=["Hulu"], type="mixed"),
    _ch(1057, "Paramountain+",      studios=["Paramount"], type="mixed"),
    _ch(1058, "H.B.Yo Min",         studios=["HBO"], type="mixed"),
    _ch(1059, "Primary Video",      studios=["Amazon"], type="mixed"),
    _ch(1060, "Viewtime",           studios=["Showtime"], type="mixed"),

    # Anime
    _ch(1061, "Munchyroll",         genres=["Animation", "Anime"], type="series"),

    # Music channels
    _ch(1062, "Tune Choice - Rock",       genres=["Rock"],                              type="music"),
    _ch(1063, "Tune Choice - Pop",        genres=["Pop"],                               type="music"),
    _ch(1064, "Tune Choice - Hip-Hop",    genres=["Hip-Hop", "Hip Hop", "Rap"],         type="music"),
    _ch(1065, "Tune Choice - Jazz",       genres=["Jazz"],                              type="music"),
    _ch(1066, "Tune Choice - Classical",  genres=["Classical"],                         type="music"),
    _ch(1067, "Tune Choice - Electronic", genres=["Electronic", "Dance", "EDM"],        type="music"),
    _ch(1068, "Tune Choice - Country",    genres=["Country"],                           type="music"),
    _ch(1069, "Tune Choice - Metal",      genres=["Metal", "Heavy Metal"],              type="music"),
    _ch(1070, "Tune Choice - Blues",      genres=["Blues"],                             type="music"),
    _ch(1071, "Tune Choice - Soundtrack", genres=["Soundtrack", "Score"],               type="music"),

    # Weather
    _ch(1072, "The Storm Channel",        type="weather"),
]

# Build a dict for O(1) lookup
PREDEFINED_BY_NUMBER: dict[int, ChannelConfig] = {
    c.number: c for c in PREDEFINED_CHANNELS
}
