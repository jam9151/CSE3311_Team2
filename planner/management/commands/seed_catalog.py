"""Populate the shared media catalog, and optionally a demo student account.

    python manage.py seed_catalog
    python manage.py seed_catalog --demo

The catalog stands in for the scraped media source described in the inception
deck. Runtimes and page-time estimates are approximate on purpose: the point of
the prototype is that the scheduler has a duration to reason about.
"""

from datetime import date, time

from django.contrib.auth import get_user_model
from django.core.management.base import BaseCommand
from django.db import transaction

from planner.models import BacklogItem, CatalogItem, MediaType, Profile, RecurringBlock

CATALOG = [
    # title, type, genre, creator, release, minutes, rating, description
    ("Dune", MediaType.MOVIE, "Science fiction", "Denis Villeneuve", date(2021, 10, 22), 155, 8.0,
     "A noble family is drawn into a war over the most valuable resource in the galaxy."),
    ("Everything Everywhere All at Once", MediaType.MOVIE, "Science fiction", "Daniels", date(2022, 3, 25), 139, 7.8,
     "A laundromat owner is pulled across parallel universes to save them all."),
    ("Spider-Man: Into the Spider-Verse", MediaType.MOVIE, "Animation", "Bob Persichetti", date(2018, 12, 14), 117, 8.4,
     "Miles Morales meets Spider-People from other dimensions."),
    ("Parasite", MediaType.MOVIE, "Thriller", "Bong Joon-ho", date(2019, 5, 30), 132, 8.5,
     "A poor family schemes its way into the household of a wealthy one."),
    ("The Grand Budapest Hotel", MediaType.MOVIE, "Comedy", "Wes Anderson", date(2014, 3, 28), 99, 8.1,
     "A concierge and his lobby boy get tangled up in a stolen painting."),
    ("Arrival", MediaType.MOVIE, "Science fiction", "Denis Villeneuve", date(2016, 11, 11), 116, 7.9,
     "A linguist is recruited to communicate with visitors who have landed on Earth."),
    ("Knives Out", MediaType.MOVIE, "Mystery", "Rian Johnson", date(2019, 11, 27), 130, 7.9,
     "A detective investigates the death of a wealthy crime novelist."),
    ("Whiplash", MediaType.MOVIE, "Drama", "Damien Chazelle", date(2014, 10, 10), 106, 8.5,
     "A young drummer is pushed to his limit by a ruthless instructor."),
    ("Coco", MediaType.MOVIE, "Animation", "Lee Unkrich", date(2017, 11, 22), 105, 8.4,
     "A boy travels to the Land of the Dead to uncover his family's history."),
    ("Blade Runner 2049", MediaType.MOVIE, "Science fiction", "Denis Villeneuve", date(2017, 10, 6), 164, 8.0,
     "A replicant blade runner uncovers a secret that could upend society."),

    ("The Bear", MediaType.TV, "Drama", "FX", date(2022, 6, 23), 1080, 8.6,
     "A fine-dining chef returns home to run his family's sandwich shop."),
    ("Severance", MediaType.TV, "Science fiction", "Apple TV+", date(2022, 2, 18), 1080, 8.7,
     "Office workers surgically divide their memories between work and home."),
    ("Arcane", MediaType.TV, "Animation", "Riot Games", date(2021, 11, 6), 990, 9.0,
     "Two sisters end up on opposite sides of a war between twin cities."),
    ("The Last of Us", MediaType.TV, "Drama", "HBO", date(2023, 1, 15), 540, 8.7,
     "A smuggler escorts a teenage girl across a collapsed United States."),
    ("Breaking Bad", MediaType.TV, "Crime", "AMC", date(2008, 1, 20), 3060, 9.5,
     "A chemistry teacher turns to manufacturing drugs after a diagnosis."),
    ("Fleabag", MediaType.TV, "Comedy", "BBC", date(2016, 7, 21), 360, 8.7,
     "A sharp, grieving woman narrates her own messy life to the camera."),
    ("Avatar: The Last Airbender", MediaType.TV, "Animation", "Nickelodeon", date(2005, 2, 21), 2460, 9.3,
     "A young airbender must master four elements to end a hundred-year war."),
    ("Andor", MediaType.TV, "Science fiction", "Disney+", date(2022, 9, 21), 720, 8.4,
     "A thief is drawn into the early days of a rebellion."),

    ("Project Hail Mary", MediaType.BOOK, "Science fiction", "Andy Weir", date(2021, 5, 4), 640, 8.7,
     "A lone astronaut wakes with amnesia aboard a ship sent to save Earth."),
    ("The Fifth Season", MediaType.BOOK, "Fantasy", "N. K. Jemisin", date(2015, 8, 4), 600, 8.3,
     "A woman searches for her daughter on a continent torn by cataclysm."),
    ("Educated", MediaType.BOOK, "Memoir", "Tara Westover", date(2018, 2, 20), 580, 8.5,
     "A woman raised off the grid in Idaho makes her way to a doctorate."),
    ("Klara and the Sun", MediaType.BOOK, "Science fiction", "Kazuo Ishiguro", date(2021, 3, 2), 500, 7.6,
     "An artificial friend observes the family that brings her home."),
    ("Piranesi", MediaType.BOOK, "Fantasy", "Susanna Clarke", date(2020, 9, 15), 400, 8.2,
     "A man lives alone in a house of endless halls and tides."),
    ("Thinking, Fast and Slow", MediaType.BOOK, "Nonfiction", "Daniel Kahneman", date(2011, 10, 25), 900, 8.1,
     "A tour of the two systems that drive how people think and decide."),
    ("The Song of Achilles", MediaType.BOOK, "Historical", "Madeline Miller", date(2011, 9, 20), 520, 8.4,
     "The Iliad retold through the friendship at its centre."),
    ("Dune (novel)", MediaType.BOOK, "Science fiction", "Frank Herbert", date(1965, 8, 1), 1100, 8.8,
     "The novel behind the films, and the start of a much longer saga."),
    ("Clean Code", MediaType.BOOK, "Technical", "Robert C. Martin", date(2008, 8, 1), 720, 7.5,
     "A widely argued-over set of habits for writing readable software."),

    ("Hades", MediaType.GAME, "Roguelike", "Supergiant Games", date(2020, 9, 17), 1320, 9.3,
     "Fight out of the underworld, one run at a time."),
    ("Stardew Valley", MediaType.GAME, "Simulation", "ConcernedApe", date(2016, 2, 26), 3000, 9.1,
     "Inherit a farm and rebuild a small town's life around it."),
    ("The Legend of Zelda: Breath of the Wild", MediaType.GAME, "Adventure", "Nintendo", date(2017, 3, 3), 3000, 9.6,
     "Wake after a hundred years and cross an open Hyrule."),
    ("Celeste", MediaType.GAME, "Platformer", "Maddy Makes Games", date(2018, 1, 25), 480, 9.1,
     "Climb a mountain, and something harder, one screen at a time."),
    ("Outer Wilds", MediaType.GAME, "Adventure", "Mobius Digital", date(2019, 5, 28), 1200, 9.2,
     "Explore a solar system stuck in a twenty-two minute time loop."),
    ("Disco Elysium", MediaType.GAME, "Role-playing", "ZA/UM", date(2019, 10, 15), 1800, 9.1,
     "A detective with no memory investigates a murder and himself."),
    ("Portal 2", MediaType.GAME, "Puzzle", "Valve", date(2011, 4, 19), 500, 9.5,
     "Solve your way out of a test facility with a portal gun."),
    ("Elden Ring", MediaType.GAME, "Role-playing", "FromSoftware", date(2022, 2, 25), 3600, 9.4,
     "A vast open world that does not explain itself."),
    ("Untitled Goose Game", MediaType.GAME, "Puzzle", "House House", date(2019, 9, 20), 180, 8.0,
     "You are a horrible goose with a to-do list."),
]


DEMO_BLOCKS = [
    ("CSE 3311 lecture", RecurringBlock.Kind.CLASS, 1, time(11, 0), time(12, 20)),
    ("CSE 3311 lecture", RecurringBlock.Kind.CLASS, 3, time(11, 0), time(12, 20)),
    ("CSE 3320 lecture", RecurringBlock.Kind.CLASS, 1, time(13, 0), time(14, 20)),
    ("CSE 3320 lecture", RecurringBlock.Kind.CLASS, 3, time(13, 0), time(14, 20)),
    ("Shift at the library", RecurringBlock.Kind.WORK, 0, time(9, 0), time(15, 0)),
    ("Shift at the library", RecurringBlock.Kind.WORK, 2, time(9, 0), time(15, 0)),
    ("Shift at the library", RecurringBlock.Kind.WORK, 4, time(9, 0), time(17, 0)),
    ("Study group", RecurringBlock.Kind.STUDY, 2, time(18, 0), time(20, 0)),
    ("Gym", RecurringBlock.Kind.OTHER, 5, time(10, 0), time(11, 30)),
]

DEMO_BACKLOG = [
    ("Dune", 5),
    ("Project Hail Mary", 4),
    ("Hades", 4),
    ("Severance", 3),
    ("Piranesi", 2),
    ("Portal 2", 3),
]


class Command(BaseCommand):
    help = "Seed the shared media catalog, and optionally a demo student account."

    def add_arguments(self, parser):
        parser.add_argument(
            "--demo",
            action="store_true",
            help="Also create a demo user (demo@mavs.uta.edu / mediaflow123) with a full week and backlog.",
        )

    @transaction.atomic
    def handle(self, *args, **options):
        created = 0
        for title, media_type, genre, creator, release, minutes, rating, description in CATALOG:
            _, was_created = CatalogItem.objects.update_or_create(
                title=title,
                media_type=media_type,
                defaults={
                    "genre": genre,
                    "creator": creator,
                    "release_date": release,
                    "typical_minutes": minutes,
                    "rating": rating,
                    "description": description,
                },
            )
            created += int(was_created)

        self.stdout.write(
            self.style.SUCCESS(f"Catalog ready: {CatalogItem.objects.count()} items ({created} new).")
        )

        if options["demo"]:
            self._create_demo_user()

    def _create_demo_user(self):
        User = get_user_model()
        user, created = User.objects.get_or_create(
            username="demo",
            defaults={"email": "demo@mavs.uta.edu", "first_name": "Demo"},
        )
        if created:
            user.set_password("mediaflow123")
            user.save()

        Profile.objects.update_or_create(
            user=user, defaults={"day_start": time(8, 0), "day_end": time(23, 30)}
        )

        user.recurring_blocks.all().delete()
        for label, kind, weekday, start, end in DEMO_BLOCKS:
            RecurringBlock.objects.create(
                user=user, label=label, kind=kind, weekday=weekday, start_time=start, end_time=end
            )

        for title, priority in DEMO_BACKLOG:
            catalog_item = CatalogItem.objects.filter(title=title).first()
            if not catalog_item:
                continue
            BacklogItem.objects.update_or_create(
                user=user,
                catalog_item=catalog_item,
                defaults={
                    "title": catalog_item.title,
                    "media_type": catalog_item.media_type,
                    "estimated_minutes": catalog_item.typical_minutes,
                    "priority": priority,
                },
            )

        # Give one item some progress so "finish what you started" has something to prefer.
        in_progress = user.backlog_items.filter(title="Project Hail Mary").first()
        if in_progress:
            in_progress.minutes_completed = 180
            in_progress.status = BacklogItem.Status.IN_PROGRESS
            in_progress.save()

        self.stdout.write(
            self.style.SUCCESS("Demo account ready: username 'demo', password 'mediaflow123'.")
        )
