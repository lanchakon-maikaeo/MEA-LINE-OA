from operations.management.demo import DemoCommand


class Command(DemoCommand):
    help = "Validate local/CI demo database before any development setup or reset."

    def handle_demo(self, *args, **options):
        self.stdout.write("Local/CI demo database guard passed.")
