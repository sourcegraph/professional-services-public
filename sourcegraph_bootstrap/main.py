import os
import getpass
import sys
import json
import requests
import time


def validate_admin_credentials():
    """Validate that SRC_ADMIN_PASS is set, prompt if not."""
    admin_pass = os.getenv('SRC_ADMIN_PASS')
    admin_user = os.getenv('SRC_ADMIN_USER')
    admin_email = os.getenv('SRC_ADMIN_EMAIL')
    
    password_from_env = bool(admin_pass)
    
    if not admin_pass:
        admin_pass = getpass.getpass("Enter admin password: ")
        os.environ['SRC_ADMIN_PASS'] = admin_pass
        print("✓ SRC_ADMIN_PASS set")

    if not admin_user:
        admin_user = input("Enter admin username: ")
        os.environ['SRC_ADMIN_USER'] = admin_user
        print("✓ SRC_ADMIN_USER set")

    if not admin_email:
        admin_email = input("Enter admin email: ")
        os.environ['SRC_ADMIN_EMAIL'] = admin_email
        print("✓ SRC_ADMIN_EMAIL set")

    return admin_user, admin_pass, admin_email, password_from_env

def validate_endpoint():
    """Check if SRC_ENDPOINT is set, prompt if not."""
    endpoint = os.getenv('SRC_ENDPOINT')
    if not endpoint:
        endpoint = input("Enter Sourcegraph URL (e.g., https://your-sourcegraph-url): ")
        os.environ['SRC_ENDPOINT'] = endpoint
        print("✓ SRC_ENDPOINT set")
    
    return endpoint

def wait_for_sourcegraph_ready(endpoint, max_retries=3, retry_delay=10):
    """Check if Sourcegraph instance needs site initialization using /sign-in endpoint."""
    sign_in_url = f"{endpoint}/sign-in"
    print(f"\nChecking if Sourcegraph needs initialization at {sign_in_url} (max {max_retries} retries, {retry_delay}s delay)\n")
    
    for attempt in range(max_retries + 1):
        try:
            response = requests.get(sign_in_url, timeout=10)
            if response.status_code == 200:
                # Check if the HTML contains the needsSiteInit flag
                if '"needsSiteInit":true' in response.text:
                    print(f"✓ Sourcegraph instance needs initialization and is ready!")
                    return True
                else:
                    print(f"✗ Sourcegraph instance is already initialized")
                    return False
        except requests.RequestException as e:
            print(f"Request failed: {e}")
        
        if attempt < max_retries:
            print(f"Attempt {attempt + 1}/{max_retries + 1} failed, retrying in {retry_delay} seconds...")
            time.sleep(retry_delay)
        else:
            print(f"✗ Sourcegraph instance failed to respond after {max_retries + 1} attempts")
            return False


def create_admin_token(endpoint, admin_user, admin_email, admin_pass):
    """Initialize site and create admin token using proper authentication flow."""
        
    if not wait_for_sourcegraph_ready(endpoint):
        return None
    
    try:
        resp = requests.get(endpoint)
        csrf_token = extract_csrf_token(resp.text)
        csrf_cookie = None
        session_cookie = None
        
        for cookie in resp.cookies:
            if cookie.name == "sg_csrf_token":
                csrf_cookie = cookie
                break
        
    except requests.RequestException as e:
        print(f"✗ Error getting CSRF token: {e}")
        return None

    site_init_url = f"{endpoint}/-/site-init"
    payload = {
        "email": admin_email,
        "username": admin_user,
        "password": admin_pass,
    }
    
    headers = {
        'Content-Type': 'application/json',
        'X-Requested-With': 'Sourcegraph'
    }
    
    if csrf_token:
        headers['X-Csrf-Token'] = csrf_token
    
    try:
        response = requests.post(
            site_init_url,
            json=payload,
            headers=headers,
            cookies=resp.cookies
        )
        
        if response.status_code != 200:
            print(f"✗ Site initialization failed: HTTP {response.status_code}")
            print(f"Response: {response.text}")
            return None
            
        for cookie in response.cookies:
            if cookie.name == "sgs":
                session_cookie = cookie
                break
        
        if not session_cookie:
            print("✗ No session cookie received from site initialization")
            return None
            
        print("✓ Site initialized successfully")
        
    except requests.RequestException as e:
        print(f"✗ Error during site initialization: {e}")
        return None
    
    return create_access_token_graphql(endpoint, session_cookie, csrf_cookie, csrf_token)


def extract_csrf_token(html_content):
    """Extract CSRF token from HTML content."""
    anchor = 'X-Csrf-Token":"'
    i = html_content.find(anchor)
    if i == -1:
        return None
    
    start = i + len(anchor)
    end = html_content.find('","', start)
    if end == -1:
        return None
        
    return html_content[start:end]


def create_access_token_graphql(endpoint, session_cookie, csrf_cookie, csrf_token):
    """Create access token using GraphQL API."""
    
    user_query = """
    query {
        currentUser {
            id
        }
    }
    """
    
    headers = {
        'Content-Type': 'application/json',
        'X-Requested-With': 'Sourcegraph'
    }
    
    cookies = {}
    if session_cookie:
        cookies[session_cookie.name] = session_cookie.value
    if csrf_cookie:
        cookies[csrf_cookie.name] = csrf_cookie.value
    
    try:
        resp = requests.post(
            f"{endpoint}/.api/graphql",
            json={'query': user_query},
            headers=headers,
            cookies=cookies
        )
        
        if resp.status_code != 200:
            print(f"✗ Failed to get user ID: HTTP {resp.status_code}")
            return None
            
        user_data = resp.json()
        if 'errors' in user_data:
            print(f"✗ GraphQL errors getting user: {user_data['errors']}")
            return None
            
        user_id = user_data['data']['currentUser']['id']
        
        token_mutation = """
        mutation createAccessToken($user: ID!, $scopes: [String!]!, $note: String!) {
            createAccessToken(user: $user, scopes: $scopes, note: $note) {
                token
            }
        }
        """
        
        variables = {
            'user': user_id,
            'scopes': ['user:all', 'site-admin:sudo'],
            'note': 'sourcegraph-bootstrap'
        }
        
        resp = requests.post(
            f"{endpoint}/.api/graphql",
            json={'query': token_mutation, 'variables': variables},
            headers=headers,
            cookies=cookies
        )
        
        if resp.status_code != 200:
            print(f"✗ Failed to create token: HTTP {resp.status_code}")
            return None
            
        token_data = resp.json()
        if 'errors' in token_data:
            print(f"✗ GraphQL errors creating token: {token_data['errors']}")
            return None
            
        token = token_data['data']['createAccessToken']['token']
        print("✓ Access token created successfully")
        return token
        
    except requests.RequestException as e:
        print(f"✗ Error creating access token: {e}")
        return None


def set_access_token(token):
    """Set the SRC_ACCESS_TOKEN environment variable."""
    if token:
        os.environ['SRC_ACCESS_TOKEN'] = token
        return True
    return False





def get_site_configuration(endpoint, token):
    """Get current site configuration via GraphQL."""
    query = """
    query {
      site {
        configuration {
          id
          effectiveContents
          validationMessages
        }
      }
    }
    """
    
    headers = {
        'Authorization': f'token {token}',
        'Content-Type': 'application/json'
    }
    
    try:
        response = requests.post(
            f"{endpoint}/.api/graphql",
            json={'query': query},
            headers=headers
        )
        
        if response.status_code == 200:
            data = response.json()
            if 'errors' in data:
                print(f"✗ GraphQL errors: {data['errors']}")
                return None
            return data['data']['site']['configuration']
        else:
            print(f"✗ Failed to get site configuration: {response.status_code}")
            return None
    except Exception as e:
        print(f"✗ Error getting site configuration: {e}")
        return None


def update_site_configuration(endpoint, token, config_id, updated_config):
    """Update site configuration via GraphQL."""
    mutation = """
    mutation UpdateSiteConfiguration($lastID: Int!, $input: String!) {
      updateSiteConfiguration(lastID: $lastID, input: $input)
    }
    """
    
    headers = {
        'Authorization': f'token {token}',
        'Content-Type': 'application/json'
    }
    
    variables = {
        'lastID': config_id,
        'input': json.dumps(updated_config)
    }
    
    try:
        response = requests.post(
            f"{endpoint}/.api/graphql",
            json={'query': mutation, 'variables': variables},
            headers=headers
        )
        
        if response.status_code == 200:
            data = response.json()
            if 'errors' in data:
                print(f"✗ GraphQL errors: {data['errors']}")
                return False
            restart_required = data['data']['updateSiteConfiguration']
            print("✓ Site configuration updated successfully")
            if restart_required:
                print("⚠ Server restart required for changes to take effect")
            return True
        else:
            print(f"✗ Failed to update site configuration: {response.status_code}")
            return False
    except Exception as e:
        print(f"✗ Error updating site configuration: {e}")
        return False


def set_external_url(endpoint, token):
    """Set the externalURL in site configuration to match SRC_ENDPOINT."""
    
    config = get_site_configuration(endpoint, token)
    if not config:
        return False
    
    try:
        # Parse current configuration - handle both string and object cases
        effective_contents = config['effectiveContents']
        if isinstance(effective_contents, str):
            # Remove JavaScript-style comments and trailing commas before parsing JSON
            import re
            clean_contents = re.sub(r'//.*$', '', effective_contents, flags=re.MULTILINE)
            # Remove trailing commas before } and ]
            clean_contents = re.sub(r',(\s*[}\]])', r'\1', clean_contents)
            current_config = json.loads(clean_contents)
        else:
            current_config = effective_contents
        
        print(f"✓ External URL set to: {endpoint}")
        current_config['externalURL'] = endpoint
        
        license_key = os.getenv('SRC_LICENSE_KEY')
        if license_key:
            current_config['licenseKey'] = license_key
            print(f"✓ License key added")
        
        success = update_site_configuration(
            endpoint, 
            token, 
            config['id'], 
            current_config
        )
        
        if success:
            return True
        else:
            print(f"✗ Failed to update configuration")
            return False
            
    except json.JSONDecodeError as e:
        print(f"✗ Error parsing site configuration JSON: {e}")
        print(f"Raw content: {config['effectiveContents'][:200]}...")
        return False
    except Exception as e:
        print(f"✗ Error setting external URL: {e}")
        return False


def main():
    print("Sourcegraph Bootstrap Tool")
    print("=" * 30)
    
    endpoint = validate_endpoint()
    admin_user, admin_pass, admin_email, password_from_env = validate_admin_credentials()
    
    token = create_admin_token(endpoint, admin_user, admin_email, admin_pass)
    if not token:
        sys.exit(1)
    
    if not set_access_token(token):
        sys.exit(1)
    
    if not set_external_url(endpoint, token):
        sys.exit(1)
    
    print("\n✓ Bootstrap completed!")
    print(f"Admin token: {token}\n")
    print("This token will not be displayed again. Please save it somewhere safe.")

if __name__ == "__main__":
    main()
