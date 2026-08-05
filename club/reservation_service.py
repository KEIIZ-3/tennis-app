from .models import Reservation


def create_reservation(**values):
    """Create every reservation through the model's canonical validation path."""
    reservation = Reservation(**values)
    reservation.full_clean()
    reservation.save()
    return reservation
