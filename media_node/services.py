import code
import gc
import os
import json
import logging
import torch
import time
import textwrap
import subprocess
from pathlib import Path
from llm_service import LLMCleaner
# Unified imports reflecting your tree structure
from utils import (
    ensure_dir, get_content_type, download_file, NpEncoder, 
    sanitize_filename, estimate_transcription_time, GPUPowerMonitor
)
from processor import ModelHandler, inference_lock, run_florence_ocr, run_whisper_audio
from yt.youtube import YoutubeFetcher

logger = logging.getLogger(__name__)

class ScraperService:
    """
    Business logic layer for processing posts, handling files, 
    and managing checkpoints.
    """
    
    @staticmethod
    def load_checkpoint(checkpoint_path):
        """Handle empty/corrupted checkpoint files gracefully."""
        if not os.path.exists(checkpoint_path):
            return set()
        
        try:
            if os.path.getsize(checkpoint_path) == 0:
                logger.info("Starting fresh (no previous checkpoint)")
                return set()
            
            with open(checkpoint_path, "r") as f:
                data = json.load(f)
            
            processed = set(data) if isinstance(data, list) else set()
            if processed:
                logger.info(f"Loaded checkpoint: {len(processed)} posts already processed")
            return processed
        except json.JSONDecodeError:
            logger.warning("Checkpoint file corrupted - resetting to empty")
            with open(checkpoint_path, "w") as f:
                json.dump([], f)
            return set()
        except Exception as e:
            logger.warning(f"Could not load checkpoint ({e}) - starting fresh")
            return set()

    @staticmethod
    def save_checkpoint(checkpoint_path, processed):
        try:
            with open(checkpoint_path, "w") as f:
                json.dump(list(processed), f)
        except Exception as e:
            logger.warning(f"Could not save checkpoint: {e}")

    @staticmethod
    def process_media_entry(media_dict, folder):
        """Download media files from dict."""
        ensure_dir(folder)
        pk = media_dict.get("pk") or media_dict.get("id") or media_dict.get("code")
        code = media_dict.get("code")
        caption = media_dict.get("caption", {}).get("text", "") if media_dict.get("caption") else ""
        user = media_dict.get("user", {}).get("username", "unknown")
        ctype = get_content_type(media_dict)

        if media_dict.get("_download_with_ytdlp"):
            from insta.ytdlp_fetcher import YtdlpInstaFetcher

            source_url = media_dict.get("_source_url") or media_dict.get("video_versions", [{}])[0].get("url")
            if source_url:
                YtdlpInstaFetcher.download_with_ytdlp(source_url, folder, code or str(pk))
            return {"shortcode": code, "caption": caption, "owner": user, "type": ctype}

        def get_url(candidates):
            return candidates[0].get("url") if candidates else None

        if ctype == "image":
            url = get_url(media_dict.get("image_versions2", {}).get("candidates", []))
            if url:
                download_file(url, os.path.join(folder, f"00_{pk}.jpg"))

        elif ctype in ["video", "reel"]:
            vds = media_dict.get("video_versions", [])
            if vds:
                download_file(vds[0].get("url"), os.path.join(folder, f"00_{pk}.mp4"))

        elif ctype == "carousel":
            for idx, child in enumerate(media_dict.get("carousel_media", [])):
                c_pk = child.get("pk", f"{pk}_{idx}")
                c_type = child.get("media_type")

                if c_type == 1:
                    url = get_url(child.get("image_versions2", {}).get("candidates", []))
                    ext = "jpg"
                elif c_type == 2:
                    url = child.get("video_versions", [{}])[0].get("url")
                    ext = "mp4"
                else:
                    continue

                if url:
                    download_file(url, os.path.join(folder, f"{idx:02d}_{c_pk}.{ext}"))

        return {"shortcode": code, "caption": caption, "owner": user, "type": ctype}

    @staticmethod
    def save_outputs(folder, results):
        """Save results to JSON and text"""
        with open(os.path.join(folder, "results.json"), "w", encoding="utf-8") as f:
            json.dump(results, f, ensure_ascii=False, indent=2, cls=NpEncoder)

        lines = []
        ordered_content = results.get("ordered_content") or []
        if ordered_content:
            for entry in ordered_content:
                label = f"--- MEDIA {entry.get('index', 0) + 1}: {entry.get('type', 'unknown').upper()} ---"
                lines.append(label)
                text = entry.get("text", "")
                if text:
                    lines.append(text)
                else:
                    lines.append("[No text extracted]")
                lines.append("")
            full = "\n".join(lines).strip()
            with open(os.path.join(folder, "combined.txt"), "w", encoding="utf-8") as f:
                f.write(full)
            return full

        if results.get("audio_transcript"):
            lines.append("--- AUDIO ---")
            lines.append(results["audio_transcript"])
            lines.append("")

        visual = []
        for entry in sorted(results.get("media", []), key=lambda x: x.get("path", "")):
            txt = entry.get("clean_text", "")
            if txt:
                visual.append(txt)

        if visual:
            lines.append("--- VISUAL ---")
            lines.append("\n\n".join(visual))

        full = "\n".join(lines)
        with open(os.path.join(folder, "combined.txt"), "w", encoding="utf-8") as f:
            f.write(full)

        return full

    @staticmethod
    def media_duration_seconds(path: str) -> int:
        try:
            result = subprocess.run(
                [
                    "ffprobe",
                    "-v",
                    "error",
                    "-show_entries",
                    "format=duration",
                    "-of",
                    "default=noprint_wrappers=1:nokey=1",
                    path,
                ],
                capture_output=True,
                text=True,
                timeout=15,
            )
            return max(int(float(result.stdout.strip() or 0)), 0)
        except Exception:
            return 0

    @classmethod
    def _process_posts_batched(cls, media_items, outdir, device=None, checkpoint_path=None,
                               combined_file_path=None, clean_output=True, progress=None):
        if device is None:
            device = "cuda" if torch.cuda.is_available() else "cpu"

        def notify(message: str, *, warning: bool = False):
            if progress:
                progress(message)
            if warning:
                logger.warning(message)
            else:
                logger.info(message)

        if not media_items:
            notify("No media items were fetched; nothing to process.", warning=True)
            return

        processed = cls.load_checkpoint(checkpoint_path) if checkpoint_path else set()
        unprocessed = []
        seen_in_batch: set = set()
        skipped = 0
        for item in media_items:
            code = item.get("code")
            if code and code not in processed and code not in seen_in_batch:
                unprocessed.append(item)
                seen_in_batch.add(code)
            else:
                skipped += 1

        if skipped > 0:
            logger.info(f"Skipped {skipped} already-processed posts")
        if not unprocessed:
            logger.info("All fetched posts were already processed!")
            return

        notify(f"Processing {len(unprocessed)} new posts...")
        handler = ModelHandler(device)
        llm_cleaner = LLMCleaner() if clean_output else None

        if not combined_file_path:
            combined_file_path = os.path.join(outdir, "all_combined.txt")
        base, ext = os.path.splitext(combined_file_path)
        cleaned_file_path = f"{base}_cleaned{ext}" if clean_output else None
        ensure_dir(os.path.dirname(combined_file_path))

        post_records = []
        image_work = []
        video_work = []

        for ordinal, media_item in enumerate(unprocessed, start=1):
            code = media_item.get("code")
            folder = os.path.join(outdir, f"post_{code}")
            try:
                notify(f"Downloading Instagram media {ordinal}/{len(unprocessed)}: {code}")
                meta = cls.process_media_entry(media_item, folder)
                results = {
                    "shortcode": code,
                    "caption": meta["caption"],
                    "type": meta["type"],
                    "media": [],
                    "ordered_content": [],
                }
                media_files = sorted(
                    [
                        path for path in Path(folder).iterdir()
                        if path.suffix.lower() in {".jpg", ".jpeg", ".png", ".webp", ".mp4", ".mov", ".m4v"}
                    ],
                    key=lambda path: path.name,
                )
                for index, fpath in enumerate(media_files):
                    entry_type = "image" if fpath.suffix.lower() in {".jpg", ".jpeg", ".png", ".webp"} else "video"
                    entry = {"index": index, "path": str(fpath), "type": entry_type, "text": ""}
                    results["ordered_content"].append(entry)
                    if entry_type == "image":
                        image_work.append(entry)
                    else:
                        video_work.append(entry)
                post_records.append({"code": code, "folder": folder, "meta": meta, "results": results})
            except Exception as e:
                logger.error(f"Failed to download/prepare {code}: {e}")

        if not post_records:
            notify("No downloaded Instagram media could be prepared for processing.", warning=True)
            return

        if image_work:
            notify(f"Running OCR for {len(image_work)} Instagram image(s)")
            monitor = GPUPowerMonitor()
            monitor.start()
            start_time = time.time()
            for index, entry in enumerate(image_work, start=1):
                notify(f"OCR image {index}/{len(image_work)}")
                with inference_lock():
                    handler.load_vlm()
                    entry["text"] = run_florence_ocr(entry["path"], handler)
            elapsed = time.time() - start_time
            logger.info(f"OCR batch processed in {int(elapsed // 60)} min {int(elapsed % 60)} sec")
            metrics = monitor.stop(elapsed)
            if metrics:
                logger.info(
                    "OCR GPU metrics: util %.1f%% | VRAM %.2f GB | power %.1f W | energy %.4f Wh",
                    metrics["avg_util_pct"],
                    metrics["max_vram_gb"],
                    metrics["avg_power_w"],
                    metrics["energy_wh"],
                )

        if video_work:
            total_duration = sum(cls.media_duration_seconds(entry["path"]) for entry in video_work)
            if total_duration:
                notify(f"Estimated transcription time: {estimate_transcription_time(total_duration)}")
            notify(f"Transcribing {len(video_work)} Instagram video(s)")
            monitor = GPUPowerMonitor()
            monitor.start()
            start_time = time.time()
            for index, entry in enumerate(video_work, start=1):
                notify(f"Transcribing video {index}/{len(video_work)}")
                with inference_lock():
                    handler.load_audio()
                    entry["text"] = run_whisper_audio(entry["path"], handler)
            elapsed = time.time() - start_time
            logger.info(f"Whisper batch processed in {int(elapsed // 60)} min {int(elapsed % 60)} sec")
            metrics = monitor.stop(elapsed)
            if metrics:
                logger.info(
                    "Whisper GPU metrics: util %.1f%% | VRAM %.2f GB | power %.1f W | energy %.4f Wh",
                    metrics["avg_util_pct"],
                    metrics["max_vram_gb"],
                    metrics["avg_power_w"],
                    metrics["energy_wh"],
                )

        combined_fh = open(combined_file_path, "a", encoding="utf-8")
        cleaned_fh = open(cleaned_file_path, "a", encoding="utf-8") if clean_output else None
        try:
            for record in post_records:
                code = record["code"]
                meta = record["meta"]
                results = record["results"]
                results["media"] = [
                    {"path": entry["path"], "type": "image", "clean_text": entry["text"]}
                    for entry in results["ordered_content"]
                    if entry["type"] == "image"
                ]
                audio_text = " ".join(
                    entry["text"]
                    for entry in results["ordered_content"]
                    if entry["type"] == "video" and entry["text"]
                ).strip()
                if audio_text:
                    results["audio_transcript"] = audio_text

                final_raw_text = cls.save_outputs(record["folder"], results)
                post_path = "reel" if meta["type"] == "reel" else "p"
                raw_block = f"Post: https://www.instagram.com/{post_path}/{code}/\n"
                raw_block += f"Type: {meta['type']}\n"
                if meta["caption"]:
                    raw_block += f"Caption: {meta['caption']}\n"
                raw_block += "-" * 40 + "\n"
                raw_block += final_raw_text + "\n"

                combined_fh.write(raw_block)
                combined_fh.write("=" * 80 + "\n\n")
                combined_fh.flush()

                if clean_output:
                    logger.info(f"Sending post {code} to LLM for cleaning...")
                    cleaned_text = llm_cleaner.clean_text(raw_block)
                    cleaned_fh.write(cleaned_text + "\n")
                    cleaned_fh.write("=" * 80 + "\n\n")
                    cleaned_fh.flush()

                processed.add(code)
                if checkpoint_path:
                    cls.save_checkpoint(checkpoint_path, processed)

                status = "Processed and Cleaned" if clean_output else "Processed"
                logger.info(f"{status} {code}")
        finally:
            combined_fh.close()
            if cleaned_fh:
                cleaned_fh.close()
        logger.info("Processing complete!")

    @classmethod
    def process_posts(cls, media_items, outdir, device=None, checkpoint_path=None,
                      combined_file_path=None, clean_output=True, progress=None):
        """
        Main orchestration function for Instagram.
        """
        return cls._process_posts_batched(
            media_items,
            outdir,
            device=device,
            checkpoint_path=checkpoint_path,
            combined_file_path=combined_file_path,
            clean_output=clean_output,
            progress=progress,
        )

        if device is None:
            device = "cuda" if torch.cuda.is_available() else "cpu"

        processed = cls.load_checkpoint(checkpoint_path) if checkpoint_path else set()

        unprocessed = []
        seen_in_batch: set = set()
        skipped = 0
        for item in media_items:
            code = item.get("code")
            if code and code not in processed and code not in seen_in_batch:
                unprocessed.append(item)
                seen_in_batch.add(code)
            else:
                skipped += 1

        if skipped > 0:
            logger.info(f"Skipped {skipped} already-processed posts")

        if not unprocessed:
            logger.info("All posts already processed!")
            return

        logger.info(f"Processing {len(unprocessed)} new posts...")

        handler = ModelHandler(device)
        
        llm_cleaner = LLMCleaner() if clean_output else None

        if not combined_file_path:
            combined_file_path = os.path.join(outdir, "all_combined.txt")
            
        base, ext = os.path.splitext(combined_file_path)
        cleaned_file_path = f"{base}_cleaned{ext}" if clean_output else None

        # Ensure the directory for the text file exists (since it might be outside outdir)
        ensure_dir(os.path.dirname(combined_file_path))

        combined_fh = open(combined_file_path, "a", encoding="utf-8")
        cleaned_fh = open(cleaned_file_path, "a", encoding="utf-8") if clean_output else None

        def process_file(fpath: Path, index: int) -> dict:
            if fpath.suffix.lower() in {".jpg", ".jpeg", ".png", ".webp"}:
                with inference_lock():
                    handler.load_vlm()
                    text = run_florence_ocr(str(fpath), handler)
                return {"index": index, "path": str(fpath), "type": "image", "text": text}
            if fpath.suffix.lower() in {".mp4", ".mov", ".m4v"}:
                with inference_lock():
                    handler.load_audio()
                    text = run_whisper_audio(str(fpath), handler)
                return {"index": index, "path": str(fpath), "type": "video", "text": text}
            return {"index": index, "path": str(fpath), "type": "unknown", "text": ""}

        def process_and_write(media_item):
            code = media_item.get("code")
            folder = os.path.join(outdir, f"post_{code}")
            
            try:
                meta = cls.process_media_entry(media_item, folder)
                results = {
                    "shortcode": code,
                    "caption": meta['caption'],
                    "type": meta['type'],
                    "media": [],
                    "ordered_content": [],
                }
                
                monitor = GPUPowerMonitor()
                monitor.start()
                start_time = time.time()

                media_files = sorted(
                    [
                        path for path in Path(folder).iterdir()
                        if path.suffix.lower() in {".jpg", ".jpeg", ".png", ".webp", ".mp4", ".mov", ".m4v"}
                    ],
                    key=lambda path: path.name,
                )
                for index, fpath in enumerate(media_files):
                    entry = process_file(fpath, index)
                    results["ordered_content"].append(entry)
                    if entry["type"] == "image":
                        results["media"].append({
                            "path": entry["path"],
                            "type": "image",
                            "clean_text": entry["text"],
                        })
                    elif entry["type"] == "video":
                        results["audio_transcript"] = (
                            (results.get("audio_transcript", "") + " " + entry["text"]).strip()
                        )

                actual_time_secs = time.time() - start_time
                metrics = monitor.stop(actual_time_secs)
                
                actual_mins = int(actual_time_secs // 60)
                actual_secs = int(actual_time_secs % 60)
                time_str = f"{actual_mins} min {actual_secs} sec" if actual_mins > 0 else f"{actual_secs} sec"
                
                logger.info(f"⏱️ Processed in {time_str}")
                
                power_str = ""
                if metrics:
                    power_str = (f"GPU Util: {metrics['avg_util_pct']:.1f}% | "
                                 f"VRAM Peak: {metrics['max_vram_gb']:.2f} GB | "
                                 f"Power: {metrics['avg_power_w']:.1f} W | "
                                 f"Energy: {metrics['energy_wh']:.4f} Wh")
                    logger.info(f"⚡ {power_str}")

                final_raw_text = cls.save_outputs(folder, results)

                # --- 1. Construct the Raw Block ---
                raw_block = f"Post: https://www.instagram.com/p/{code}/\n"
                raw_block += f"Type: {meta['type']}\n"
                if meta['caption']: 
                    raw_block += f"Caption: {meta['caption']}\n"
                
                raw_block += "-" * 40 + "\n"
                raw_block += final_raw_text + "\n"
                
                # --- 2. Write Raw to standard file ---
                combined_fh.write(raw_block)
                combined_fh.write("=" * 80 + "\n\n")
                combined_fh.flush()

                if clean_output:
                    logger.info(f"✨ Sending post {code} to LLM for cleaning...")
                    cleaned_text = llm_cleaner.clean_text(raw_block)

                    cleaned_fh.write(cleaned_text + "\n")
                    cleaned_fh.write("=" * 80 + "\n\n")
                    cleaned_fh.flush()

                processed.add(code)
                if checkpoint_path:
                    cls.save_checkpoint(checkpoint_path, processed)

                status = "Processed and Cleaned" if clean_output else "Processed"
                logger.info(f"✓ {status} {code}")
            except Exception as e:
                logger.error(f"Failed {code}: {e}")

        for media in unprocessed:
            process_and_write(media)

        combined_fh.close()
        if cleaned_fh:
            cleaned_fh.close()
        logger.info("✓ Processing complete!")
        
class YoutubeService:
    """
    Business logic layer for processing YouTube videos with memory and disk cleanup.
    """
    @classmethod
    def process_video(cls, url, outdir, device=None, handler=None, progress=None):
        if device is None:
            device = "cuda" if torch.cuda.is_available() else "cpu"

        def notify(message):
            if progress:
                progress(message)
            logger.info(message)

        ensure_dir(outdir)
        audio_path = None
        owns_handler = handler is None
        
        try:
            notify(f"Fetching audio from YouTube: {url}")
            fetcher = YoutubeFetcher(outdir)
            audio_path, title, quality_str, duration = fetcher.download_audio(url)
            notify(f"Audio ready: {title}")

            # NEW: Only create a new handler if one wasn't passed in
            if owns_handler:
                handler = ModelHandler(device)
                
            notify("Preparing YouTube audio transcription")
            
            if duration:
                vid_mins = duration // 60
                vid_secs = duration % 60
                notify(f"Video length: {vid_mins}m {vid_secs}s")
                notify(f"Estimated transcription time: {estimate_transcription_time(duration)}")
            
            # This will now safely SKIP loading if the model is already in VRAM
            notify("Loading Whisper model")
            monitor = GPUPowerMonitor()
            monitor.start()
            start_time = time.time()
            with inference_lock():
                handler.load_audio()
                notify("Transcribing audio")
                transcript = run_whisper_audio(audio_path, handler)
            
            actual_time_secs = time.time() - start_time
            metrics = monitor.stop(actual_time_secs)
            
            actual_mins = int(actual_time_secs // 60)
            actual_secs = int(actual_time_secs % 60)
            actual_time_str = f"{actual_mins} min {actual_secs} sec" if actual_mins > 0 else f"{actual_secs} sec"
            
            notify(f"Actual transcription time: {actual_time_str}")
            if duration:
                rtf = actual_time_secs / max(duration, 1)
                notify(f"Transcription speed: {rtf:.2f}x video length")
            
            power_str = ""
            if metrics:
                power_str = (f"GPU Util: {metrics['avg_util_pct']:.1f}% | "
                             f"VRAM Peak: {metrics['max_vram_gb']:.2f} GB | "
                             f"Power: {metrics['avg_power_w']:.1f} W | "
                             f"Energy: {metrics['energy_wh']:.4f} Wh")
                logger.info(f"⚡ {power_str}")

            if metrics and metrics.get("max_vram_gb", 0) >= 3.7:
                notify("VRAM headroom is low for Whisper medium on a 4GB GPU")

            if transcript:
                safe_title = sanitize_filename(title)
                if not safe_title: safe_title = "youtube_transcription_unknown"
                transcript_file = os.path.join(outdir, f"{safe_title}.txt")
                
                wrapped_transcript = textwrap.fill(transcript, width=80)
                
                with open(transcript_file, "w", encoding="utf-8") as f:
                    f.write(f"Title: {title}\n")
                    f.write(f"URL: {url}\n")
                    f.write(f"Original Audio Quality: {quality_str}\n")
                    f.write("-" * 80 + "\n")
                    f.write(wrapped_transcript + "\n")
                    f.write("-" * 80 + "\n")
                    
                notify(f"Transcript saved: {os.path.basename(transcript_file)}")
            else:
                notify("No transcript was generated for this video")
                logger.warning("No transcript was generated for this video.")

        except Exception as e:
            logger.error(f"Failed to process YouTube video: {e}")
            raise
            
        finally:
            if audio_path and os.path.exists(audio_path):
                try: os.remove(audio_path)
                except Exception: pass
            if owns_handler and handler is not None:
                try:
                    handler.audio_model = None
                    gc.collect()
                    if torch.cuda.is_available():
                        torch.cuda.empty_cache()
                except Exception:
                    pass
