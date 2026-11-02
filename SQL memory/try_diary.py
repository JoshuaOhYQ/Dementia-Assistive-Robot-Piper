import diary

print(diary.window_bounds("today"))
print(diary.window_bounds("yesterday"))
print(diary.window_bounds("this_morning"))
print(diary.window_bounds("last_week"))

# The full question: "did I take my medicine today?"
bounds = diary.window_bounds("today")
if bounds is None:
    print("I don't understand that time.")
else:
    start, end = bounds
    rows = diary.events_between("medication", start, end)
    print(rows)