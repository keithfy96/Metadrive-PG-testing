should_print_in_controls = True


def cprint(*args, **kwargs):
  if should_print_in_controls:
    print(*args, **kwargs)
