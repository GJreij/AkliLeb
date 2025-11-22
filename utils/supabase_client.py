import os
from supabase import create_client

# --- Supabase setup ---
SUPABASE_URL = os.getenv("SUPABASE_URL")
SUPABASE_KEY = os.getenv("SUPABASE_KEY")

# SUPABASE_URL = 'https://rjaypqibeymfopncjxkz.supabase.co'
# SUPABASE_KEY = 'eyJhbGciOiJIUzI1NiIsInR5cCI6IkpXVCJ9.eyJpc3MiOiJzdXBhYmFzZSIsInJlZiI6InJqYXlwcWliZXltZm9wbmNqeGt6Iiwicm9sZSI6InNlcnZpY2Vfcm9sZSIsImlhdCI6MTc1NDMwOTI0MiwiZXhwIjoyMDY5ODg1MjQyfQ.CCJT_Z7ZSYNzpcIyMhC0O7z8gVdaFEl-yj1cex2zxYY'


supabase = create_client(SUPABASE_URL, SUPABASE_KEY)