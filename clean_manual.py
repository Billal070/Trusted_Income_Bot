import pathlib, re
p = pathlib.Path("D:/NoobDevs/Trusted Income Bot/user_bot.py")
t = p.read_text(encoding="utf-8")
# Remove the unreachable manual fallback block
old = "        # Manual admin fallback removed per spec - no deposit creation\n        dep_id = db.create_deposit(user.id, username, pretty, amount, trx) # unreachable\n        # clear state\n        context.user_data.pop(\"deposit_method\", None)\n        context.user_data.pop(\"deposit_amount\", None)\n        context.user_data.pop(\"deposit_step\", None)\n        await update.message.reply_text(\n            f\"\\u23f3 Your deposit request of {amount} BDT via {pretty} (TrxID: {trx}) has been submitted! Waiting for Admin verification.\"\n        )\n"
# Find and remove the large admin forwarding block that is now unreachable - it starts after the above and goes until the next return True before the final return False
# Simpler: remove everything from "# Manual admin fallback" to the next "        return True\n    return False" and keep only one return
pattern = r"        # Manual admin fallback removed.*?return True\n"
import re
t2, n = re.subn(pattern, "", t, flags=re.S)
print(f"removed {n} manual blocks")
# Also need to ensure the file still has the correct structure - the second error return should remain
p.write_text(t2, encoding="utf-8")
print("cleaned")
