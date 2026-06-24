import sys
import argparse
import logging
import torch
import os

from processor import ModelHandler
from utils import ensure_dir,sanitize_filename
from services import ScraperService, YoutubeService
from llm_service import LLMCleaner # NEW IMPORT
"""USAGE:
Mode	Implementation
--private-user	WebCollectionFetcher (Playwright browser + web API interception)
--public-user	YtdlpInstaFetcher (yt-dlp)
--post	YtdlpInstaFetcher (yt-dlp)
--ig-bulk	YtdlpInstaFetcher (yt-dlp)
--youtube	YoutubeService (unchanged)
Usage:

# Saved collection (first run — opens browser for login):
python -m main --username sanath_navada --password S9dqJ2zPx_XLDwj --private-user Mind --outdir ./data/my_collections

# Saved collection (subsequent runs — no credentials needed):
python -m main --private-user Mind --outdir ./data/my_collections

# Public user feed (no auth needed):
python -m main --public-user gingerpotter21 --first-n 3 --outdir ./data/public

# Single post:
python -m main --post https://www.instagram.com/p/Cqd7ZJdDLLj/ --outdir ./data/posts

# Bulk URLs from file:

 # YouTube:
 python -m main --youtube "data/my_transcripts/urls.txt" --outdir "./data/my_transcripts"
 
 python -m main --clean-bulk ./data/my_collections/test.txt
"""

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
logger = logging.getLogger(__name__)


def load_dotenv(path=".env"):
    if not os.path.exists(path):
        return
    with open(path, encoding="utf-8") as f:
        for raw_line in f:
            line = raw_line.strip()
            if not line or line.startswith("#") or "=" not in line:
                continue
            key, value = line.split("=", 1)
            os.environ.setdefault(key.strip(), value.strip().strip('"').strip("'"))


def main():
    load_dotenv(os.path.join(os.path.dirname(os.path.dirname(__file__)), ".env"))
    parser = argparse.ArgumentParser(
        description='Scraper Suite - Instagram & YouTube Production Pipeline',
        formatter_class=argparse.RawDescriptionHelpFormatter
    )

    mode_group = parser.add_mutually_exclusive_group(required=True)
    mode_group.add_argument("--public-user", metavar="USERNAME")
    mode_group.add_argument("--post",        metavar="URL")
    mode_group.add_argument("--private-user", metavar="COLLECTION",
                            help="Fetch a saved collection via browser (no mobile API)")
    mode_group.add_argument("--youtube",     metavar="URL_OR_FILE")
    mode_group.add_argument("--ig-bulk",     metavar="FILE",
                            help="Text file containing Instagram post URLs")
    mode_group.add_argument("--clean-bulk",  metavar="FILE",
                            help="Process an existing raw text file through the LLM cleaner")
    parser.add_argument("--username",  help="Your Instagram username")
    parser.add_argument("--password",  help="Your Instagram password")
    parser.add_argument("--totp",      metavar="SECRET",
                        help="Base32 TOTP secret for automatic 2FA (instagrapi modes only)")
    parser.add_argument("--sessionid", help="Inject browser sessionid cookie (instagrapi modes only)")
    parser.add_argument("--proxy",     help="Proxy URL (instagrapi modes only)")

    parser.add_argument("--first-n",   type=int, default=0)
    parser.add_argument("--last-n",    type=int, default=0)
    parser.add_argument("--outdir")
    parser.add_argument("--checkpoint", default="checkpoint.json")

    args = parser.parse_args()
    
    
    base_dir = args.outdir if args.outdir else "./data/insta"
    combined_file_path = None
    session_dir = None

    if args.private_user:
        safe_name = sanitize_filename(args.private_user)
        session_dir = os.path.join(base_dir, "my_collections")
        outdir = os.path.join(session_dir, safe_name)
        combined_file_path = os.path.join(session_dir, f"{safe_name}.txt")

    elif args.public_user:
        safe_name = sanitize_filename(args.public_user)
        outdir = os.path.join(base_dir, "public_users", safe_name)
        combined_file_path = os.path.join(base_dir, "public_users", f"{safe_name}_posts.txt")

    elif args.post:
        outdir = os.path.join(base_dir, "individual_posts")
        combined_file_path = os.path.join(base_dir, "individual_posts", "posts_combined.txt")

    elif args.ig_bulk:
        outdir = os.path.join(base_dir, "bulk_posts")
        combined_file_path = os.path.join(base_dir, "bulk_posts", "bulk_combined.txt")

    elif args.youtube:
        outdir = args.outdir if args.outdir else "./data/youtube"
        # YouTube doesn't use the combined file logic yet, handled in YoutubeService

    if not args.clean_bulk:
        ensure_dir(outdir)
        checkpoint_path = os.path.join(outdir, args.checkpoint)

    
    
    
    
    
    
    
    
    
    
    if args.clean_bulk:
        logger.info(f"=== MODE: LLM Bulk Cleaning ({args.clean_bulk}) ===")
        if not os.path.isfile(args.clean_bulk):
            logger.error(f"File not found: {args.clean_bulk}")
            sys.exit(1)
            
        # Create output filename (e.g., all_combined_cleaned.txt)
        base, ext = os.path.splitext(args.clean_bulk)
        output_filepath = f"{base}_cleaned{ext}"
        
        cleaner = LLMCleaner()
        cleaner.clean_bulk_file(args.clean_bulk, output_filepath)
        return
    # --private-user uses the browser fetcher.
    # Credentials are only needed on the very first run to log in; after that
    # the session is cached in web_session.json and no flags are required.
    session_cache = os.path.join(args.outdir, "web_session.json")
    if args.private_user and not os.path.exists(session_cache):
        if not (args.username and args.password):
            parser.error(
                "--private-user requires --username and --password on first run "
                "(the session is cached afterwards — no credentials needed next time)"
            )

    # For collection mode, fetch everything and let the checkpoint decide what's new.
    # For public user / yt-dlp modes, cap at 50 to avoid huge profile fetches.
    if not args.private_user:
        if args.first_n == 0 and args.last_n == 0:
            args.first_n = 50

    ensure_dir(args.outdir)
    device = "cuda" if torch.cuda.is_available() else "cpu"
    checkpoint_path = os.path.join(args.outdir, args.checkpoint)

    if torch.cuda.is_available():
        logger.info("CUDA is available. Using GPU for processing.")

    # ------------------------------------------------------------------ #
    #  YouTube                                                             #
    # ------------------------------------------------------------------ #
    if args.youtube:
        logger.info("=== MODE: YouTube Video(s) ===")
        shared_handler = ModelHandler(device)
        if os.path.isfile(args.youtube):
            with open(args.youtube, 'r') as f:
                urls = [line.strip() for line in f if line.strip() and not line.startswith('#')]
            for i, url in enumerate(urls, 1):
                logger.info(f"\n--- Processing Video {i}/{len(urls)} ---")
                YoutubeService.process_video(url, args.outdir, device, handler=shared_handler)
        else:
            YoutubeService.process_video(args.youtube, args.outdir, device, handler=shared_handler)
        return

    # ------------------------------------------------------------------ #
    #  Saved collection — browser-based (no instagrapi / mobile API)      #
    # ------------------------------------------------------------------ #
    if args.private_user:
        logger.info(f"=== MODE: Collection ('{args.private_user}') ===")
        from insta.web_fetcher import WebCollectionFetcher
        web = WebCollectionFetcher(
            outdir=args.outdir,
            session_dir=session_dir,
            username=args.username,   # None is fine if session is cached
            password=args.password,
        )
        media_items = web.fetch_collection(args.private_user,
                                           first_n=args.first_n, last_n=args.last_n)
        ScraperService.process_posts(media_items, args.outdir, device, checkpoint_path)
        return

    # ------------------------------------------------------------------ #
    #  Public Instagram content — all via yt-dlp (no mobile API)         #
    # ------------------------------------------------------------------ #
    from insta.ytdlp_fetcher import YtdlpInstaFetcher
    scraping_path = os.getenv("SCRAPING_PATH", "playwright")
    logger.info(f"Instagram scraping path: {scraping_path}")
    fetcher = YtdlpInstaFetcher(scraping_path=scraping_path, session_dir=args.outdir)

    if args.ig_bulk:
        logger.info(f"=== MODE: Instagram Bulk File ({args.ig_bulk}) ===")
        if not os.path.isfile(args.ig_bulk):
            logger.error(f"File not found: {args.ig_bulk}")
            sys.exit(1)
        with open(args.ig_bulk, 'r') as f:
            urls = [line.strip() for line in f if line.strip() and not line.startswith('#')]
        logger.info(f"Found {len(urls)} URLs to process.")
        media_items = fetcher.fetch_bulk(urls)
        if media_items:
            ScraperService.process_posts(media_items, args.outdir, device, checkpoint_path)
        else:
            logger.warning("No media items were successfully fetched.")

    elif args.public_user:
        logger.info(f"=== MODE: Public User (@{args.public_user}) ===")
        
        # Apply the default 50 limit if not specified
        if args.first_n == 0 and args.last_n == 0:
            args.first_n = 50
            
        media_items = fetcher.fetch_user_posts(args.public_user, first_n=args.first_n, last_n=args.last_n)
        ScraperService.process_posts(media_items, outdir, device, checkpoint_path, combined_file_path)

    elif args.post:
        logger.info("=== MODE: Single Post ===")
        media_items = fetcher.fetch_single_post(args.post)
        if media_items:
            ScraperService.process_posts(media_items, outdir, device, checkpoint_path, combined_file_path)
        else:
            logger.error("Could not fetch the post.")
            sys.exit(1)


if __name__ == "__main__":
    main()
