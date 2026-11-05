"""One module per job-board type.

Every module exposes the same two functions:

    list_jobs(company, settings) -> list[Job]
        One pass over the board's public listing. Lever and Ashby include full
        descriptions here; Greenhouse and Workday return titles only.

    fill_details(company, job, settings) -> None
        Fetch whatever list_jobs left out (description, real posted date).
        Only called for new jobs whose title already passed the filter.
"""

from boards import ashby, greenhouse, lever, smartrecruiters, workday

BOARDS = {
    "greenhouse": greenhouse,
    "lever": lever,
    "ashby": ashby,
    "workday": workday,
    "smartrecruiters": smartrecruiters,
}
