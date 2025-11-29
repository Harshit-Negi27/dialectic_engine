from youtube_transcript_api import YouTubeTranscriptApi
from urllib.parse import urlparse, parse_qs

def get_video_id(url: str) -> str:
    """Extracts the video ID from a YouTube URL"""
    try:
        parsed = urlparse(url)
        if parsed.hostname == 'youtu.be':
            return parsed.path[1:]
        if parsed.hostname in ('www.youtube.com', 'youtube.com'):
            if parsed.path == '/watch':
                return parse_qs(parsed.query)['v'][0]
            if parsed.path.startswith('/shorts/'):
                return parsed.path.split('/')[2]
    except:
        return None
    return None

def get_transcript_text(video_url: str) -> str:
    video_id = get_video_id(video_url)
    if not video_id: return None
    
    print(f" Fetching transcript for: {video_id}")
    try:
        ytt = YouTubeTranscriptApi()
        
        # Get the list of all available transcripts
        transcript_list = ytt.list(video_id)
        
        transcript = None
        
        
        try:
            transcript = transcript_list.find_transcript(['en', 'en-US', 'en-GB'])
        except:
            try:
                transcript = transcript_list.find_generated_transcript(['en', 'en-US', 'en-GB'])
            except:
                # Fallback
                print(" No English found. falling back to first available language.")
                for t in transcript_list:
                    transcript = t
                    break

        if not transcript:
            return None

        # Fetch the data
        full_data = transcript.fetch()
        
        # Combine text
        full_text = " ".join([item.text for item in full_data])
        return full_text

    except Exception as e:
        print(f" Error details: {e}")
        return None