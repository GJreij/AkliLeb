import os
from supabase import create_client

# --- Supabase setup ---
SUPABASE_URL = os.getenv("SUPABASE_URL")
SUPABASE_KEY = os.getenv("SUPABASE_KEY")

# SUPABASE_URL = 'https://rjaypqibeymfopncjxkz.supabase.co'
# SUPABASE_KEY = '***REMOVED-LEAKED-SUPABASE-SERVICE-ROLE-KEY***'


supabase = create_client(SUPABASE_URL, SUPABASE_KEY)