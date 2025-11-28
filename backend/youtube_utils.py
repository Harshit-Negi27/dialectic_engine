from youtube_transcript_api import YouTubeTranscriptApi
from urllib.parse import urlparse, parse_qs

def get_video_id(url: str) -> str:
    """Extracts the video ID from a YouTube URL (including Shorts)."""
    try:
        parsed = urlparse(url)
        
        # Case 1: Shortened URL (youtu.be/ID)
        if parsed.hostname == 'youtu.be':
            return parsed.path[1:]
        
        if parsed.hostname in ('www.youtube.com', 'youtube.com'):
            # Case 2: Standard Video (youtube.com/watch?v=ID)
            if parsed.path == '/watch':
                return parse_qs(parsed.query)['v'][0]
            
            # Case 3: YouTube Shorts (youtube.com/shorts/ID)
            if parsed.path.startswith('/shorts/'):
                return parsed.path.split('/')[2]
                
    except:
        return None
    return None

def get_transcript_text(video_url: str) -> str:
    """Fetches transcript using v1.2.x syntax (Objects, not Dicts)."""
    video_id = get_video_id(video_url)
    if not video_id: return None
    
    print(f"🎥 Fetching transcript for: {video_id}")
    try:
        # 1. Instantiate
        ytt = YouTubeTranscriptApi()
        
        # 2. List
        transcript_list = ytt.list(video_id)
        
        # 3. Find English
        try:
            transcript = transcript_list.find_transcript(['en', 'en-US'])
        except:
            transcript = transcript_list.find_generated_transcript(['en', 'en-US'])
            
        # 4. Fetch (Returns list of Objects now, not Dicts)
        full_data = transcript.fetch()
        
        # 5. Join (Using Dot Notation)
        full_text = " ".join([item.text for item in full_data])
        return full_text

    except Exception as e:
        print(f"❌ Error details: {e}")
        return None