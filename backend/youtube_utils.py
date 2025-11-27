from youtube_transcript_api import YouTubeTranscriptApi
from urllib.parse import urlparse, parse_qs

def get_video_id(url: str) -> str:
    """Extracts the video ID from a YouTube URL."""

    parsed = urlparse(url)
    
    if parsed.hostname == 'youtu.be':
        return parsed.path[1:]
    
    if parsed.hostname in ('www.youtube.com', 'youtube.com'):
        if parsed.path == '/watch':
            p = parse_qs(parsed.query)
            return p['v'][0]
    
    return None

def get_transcript_text(video_url: str) -> str:
    """Fetches and combines the transcript text from a YouTube video."""
    try:
        video_id = get_video_id(video_url)
        if not video_id:
            return None
            
        # Fetch the transcript 
        transcript_list = YouTubeTranscriptApi.get_transcript(video_id)
        
        # Combine all the text parts into one big string
        full_text = " ".join([item['text'] for item in transcript_list])
        return full_text
        
    except Exception as e:
        print(f"Error fetching transcript: {e}")
        return None