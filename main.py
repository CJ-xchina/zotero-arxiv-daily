import arxiv
import argparse
import os
from pyzotero import zotero
from recommender import rerank_paper
from construct_email import render_email
import requests
import datetime
import re
from time import sleep
from email.header import Header
from email.mime.text import MIMEText
from email.utils import parseaddr, formataddr
import smtplib
from tldr import get_paper_tldr
from llama_cpp import Llama
from tqdm import tqdm, trange
from loguru import logger

def get_zotero_corpus(id:str,key:str) -> list[dict]:
    zot = zotero.Zotero(id, 'user', key)
    corpus = zot.everything(zot.items(itemType='conferencePaper || journalArticle || preprint'))
    corpus = [c for c in corpus if c['data']['abstractNote'] != '']
    return corpus

def get_paper_code_url(paper:arxiv.Result) -> str:
    retry_num = 5
    while retry_num > 0:
        try:
            paper_list = requests.get(f'https://paperswithcode.com/api/v1/papers/?arxiv_id={paper.arxiv_id}').json()
            break
        except:
            sleep(1)
            retry_num -= 1
            if retry_num == 0:
                return None
    
    if paper_list.get('count',0) == 0:
        return None
    paper_id = paper_list['results'][0]['id']
    retry_num = 5
    while retry_num > 0:
        try:
            repo_list = requests.get(f'https://paperswithcode.com/api/v1/papers/{paper_id}/repositories/').json()
            break
        except:
            sleep(1)
            retry_num -= 1
            if retry_num == 0:
                return None
    if repo_list.get('count',0) == 0:
        return None
    return repo_list['results'][0]['url']

def get_arxiv_paper_from_web(query:str, start:datetime.datetime, end:datetime.datetime) -> list[arxiv.Result]:
    cats = re.findall(r'cat:(\w+)?\.\w+?', query)
    cats = set(cats)
    query_list = query.split(' ')
    real_query = []
    for q in query_list:
        if q in ["OR","AND","ANDNOT"]:
            if real_query[-1] in ["OR","AND","ANDNOT"]:
                #This means previous filter is skipped
                real_query.pop()
            real_query.append(q)
        if q.startswith("cat:"):
            real_query.append(q)

    if real_query[-1] in ["OR","AND","ANDNOT"]:
        real_query.pop()

    logger.info(f"Retrieving arXiv papers from {start} to {end} with {' '.join(real_query)}. Other query filters are ignored.")
    all_paper_ids = []
    for cat in cats:
        url = f"https://arxiv.org/list/{cat}/new" #! This only retrieves the latest papers submitted in yesterday
        response = requests.get(url)
        if response.status_code != 200:
            logger.warning(f"Cannot retrieve papers from {url}.")
            continue
        html = response.text
        paper_ids = re.findall(r'arXiv:(\d+\.\d+)', html)
        all_paper_ids.extend(paper_ids)

    def is_valid(paper:arxiv.Result):
        published_date = paper.published
        if not (published_date < end and published_date >= start):
            return False
        stack = []
        op_dict = {
            "AND": lambda x,y: x and y,
            "OR": lambda x,y: x or y,
            "ANDNOT": lambda x,y: x and not y
        }
        for q in real_query:
            if q.startswith("cat:"):
                if len(stack) == 0:
                    stack.append(q[4:] in paper.categories)
                else:
                    op = stack.pop()
                    x = stack.pop()
                    assert op in ["AND","OR","ANDNOT"], f"Invalid query {query}"
                    assert x in [True,False], f"Invalid query {query}"
                    stack.append(op_dict[op](x,q[4:] in paper.categories))
            elif q in ["AND","OR","ANDNOT"]:
                stack.append(q)
        assert len(stack) == 1 and (stack[0] in [True, False]), f"Invalid query {query}"
        return stack.pop()
    
    client = arxiv.Client()
    results = []
    for i in trange(0,len(all_paper_ids),50,desc="Filtering papers"):
        search = arxiv.Search(id_list=all_paper_ids[i:i+50])
        for i in client.results(search):
            if is_valid(i):
                i.arxiv_id = re.sub(r'v\d+$', '', i.get_short_id())
                i.code_url = get_paper_code_url(i)
                results.append(i)
    return results 
        

def get_arxiv_paper(query:str, start:datetime.datetime, end:datetime.datetime, debug:bool=False) -> list[arxiv.Result]:
    client = arxiv.Client()
    search = arxiv.Search(query=query, sort_by=arxiv.SortCriterion.SubmittedDate)
    retry_num = 5
    if not debug:
        while retry_num > 0:
            papers = []
            try:
                for i in client.results(search):
                    published_date = i.published
                    if published_date < end and published_date >= start:
                        i.arxiv_id = re.sub(r'v\d+$', '', i.get_short_id())
                        i.code_url = get_paper_code_url(i)
                        papers.append(i)
                    elif published_date < start:
                        break
                break
            except Exception as e:
                logger.warning(f'Got error: {e}. Try again...')
                sleep(180)
                retry_num -= 1
                if retry_num == 0:
                    raise e
        if len(papers) == 0:
            logger.warning("Cannot retrieve new papers from arXiv API. Try to retrieve from web page.")
            papers = get_arxiv_paper_from_web(query, start, end)
    else:
        logger.debug("Retrieve 5 arxiv papers regardless of the date.")
        while retry_num > 0:
            papers = []
            try:
                for i in client.results(search):
                    i.arxiv_id = re.sub(r'v\d+$', '', i.get_short_id())
                    i.code_url = get_paper_code_url(i)
                    papers.append(i)
                    if len(papers) == 5:
                        break
                break
            except Exception as e:
                logger.warning(f'Got error: {e}. Try again...')
                sleep(180)
                retry_num -= 1
                if retry_num == 0:
                    raise e
    return papers

def send_email(sender:str, receiver:str, password:str,smtp_server:str,smtp_port:int, html:str,):
    def _format_addr(s):
        name, addr = parseaddr(s)
        return formataddr((Header(name, 'utf-8').encode(), addr))

    msg = MIMEText(html, 'html', 'utf-8')
    msg['From'] = _format_addr('Github Action <%s>' % sender)
    msg['To'] = _format_addr('You <%s>' % receiver)
    today = datetime.datetime.now().strftime('%Y/%m/%d')
    msg['Subject'] = Header(f'Daily arXiv {today}', 'utf-8').encode()

    try:
        server = smtplib.SMTP(smtp_server, smtp_port)
        server.starttls()
    except smtplib.SMTPServerDisconnected:
        server = smtplib.SMTP_SSL(smtp_server, smtp_port)

    server.login(sender, password)
    server.sendmail(sender, [receiver], msg.as_string())
    server.quit()

if __name__ == '__main__':
    parser = argparse.ArgumentParser(description='Recommender system for academic papers')
    parser.add_argument('--zotero_id', type=str, help='Zotero user ID',default=os.environ.get('ZOTERO_ID'))
    parser.add_argument('--zotero_key', type=str, help='Zotero API key',default=os.environ.get('ZOTERO_KEY'))
    parser.add_argument('--max_paper_num', type=int, help='Maximum number of papers to recommend',default=os.environ.get('MAX_PAPER_NUM',100))
    parser.add_argument('--arxiv_query', type=str, help='Arxiv search query',default=os.environ.get('ARXIV_QUERY'))
    parser.add_argument('--smtp_server', type=str, help='SMTP server',default=os.environ.get('SMTP_SERVER'))
    parser.add_argument('--smtp_port', type=int, help='SMTP port',default=os.environ.get('SMTP_PORT'))
    parser.add_argument('--sender', type=str, help='Sender email address',default=os.environ.get('SENDER'))
    parser.add_argument('--receiver', type=str, help='Receiver email address',default=os.environ.get('RECEIVER'))
    parser.add_argument('--password', type=str, help='Sender email password',default=os.environ.get('SENDER_PASSWORD'))
    parser.add_argument('--debug', action='store_true', help='Debug mode')
    args = parser.parse_args()
    assert args.zotero_id is not None
    assert args.zotero_key is not None
    assert args.arxiv_query is not None
    if args.debug:
        logger.debug("Debug mode is on.")
    today = datetime.datetime.now(tz=datetime.timezone.utc).replace(hour=0, minute=0, second=0, microsecond=0)
    yesterday = today - datetime.timedelta(days=1)
    logger.info("Retrieving Zotero corpus...")
    corpus = get_zotero_corpus(args.zotero_id, args.zotero_key)
    logger.info(f"Retrieved {len(corpus)} papers from Zotero.")
    logger.info("Retrieving Arxiv papers...")
    papers = get_arxiv_paper(args.arxiv_query, yesterday, today, args.debug)
    if len(papers) == 0:
        logger.info("No new papers found. Yesterday maybe a holiday and no one submit their work :). If this is not the case, please check the ARXIV_QUERY.")
        logger.info("No email will be sent. Enjoy a relaxing day!")
        exit(0)
    logger.info("Reranking papers...")
    papers = rerank_paper(papers, corpus)
    if args.max_paper_num != -1:
        papers = papers[:args.max_paper_num]
    
    logger.info("Generating TLDRs...")
    llm = Llama.from_pretrained(
        repo_id="Qwen/Qwen2.5-3B-Instruct-GGUF",
        filename="qwen2.5-3b-instruct-q4_k_m.gguf",
        n_ctx=4096,
        n_threads=4,
        verbose=False
    )
    for p in tqdm(papers):
        p.tldr = get_paper_tldr(p, llm)

    html = render_email(papers)
    logger.info("Sending email...")
    send_email(args.sender, args.receiver, args.password, args.smtp_server, args.smtp_port, html)
    logger.success("Email sent successfully! If you don't receive the email, please check the configuration and the junk box.")
